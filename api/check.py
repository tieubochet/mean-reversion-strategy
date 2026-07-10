"""
=============================================================================
Endpoint: GET/POST /api/check — PAIRS TRADING STATUS CHECK (CL vs BRENTOIL)
=============================================================================
Quét tín hiệu hiện tại theo yêu cầu thủ công, bất cứ lúc nào. LUÔN gửi 1 tin
Telegram báo trạng thái (dù có tín hiệu vào lệnh hay không) — khác api/index.py
chỉ gửi khi thật sự đủ điều kiện vào lệnh.

QUAN TRỌNG: file này KHÔNG import từ file .py nào khác trong repo (kể cả
api/index.py) — xem giải thích đầy đủ trong docstring của api/index.py. Toàn
bộ logic được lặp lại nguyên vẹn ở đây có chủ đích, để tránh lỗi
ModuleNotFoundError khi Vercel bundle từng function độc lập.

ENV VARS: giống hệt api/index.py (đọc chung 1 bộ Environment Variables trên
Vercel — thay đổi env var áp dụng cho cả 2 endpoint cùng lúc).
=============================================================================
"""

import os
import time
import json
import requests
from http.server import BaseHTTPRequestHandler

# =============================================================================
# CONFIG
# =============================================================================

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LEG_A_SYMBOL = "xyz:CL"
LEG_B_SYMBOL = "xyz:BRENTOIL"
INTERVAL = "15m"
HIP3_DEX = "xyz"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

SPREAD_MEAN = float(os.environ.get("SPREAD_MEAN", "-3.2858"))
SPREAD_STD = float(os.environ.get("SPREAD_STD", "0.4675"))
SIGNAL_THRESHOLD = float(os.environ.get("SIGNAL_THRESHOLD", "1.5"))
EXIT_Z_THRESHOLD = float(os.environ.get("EXIT_Z_THRESHOLD", "0.0"))

CAPITAL_PER_LEG = float(os.environ.get("CAPITAL_PER_LEG", "5000"))
FEE_BPS_PER_FILL = float(os.environ.get("FEE_BPS_PER_FILL", "2.2"))
FILLS_PER_ROUND = int(os.environ.get("FILLS_PER_ROUND", "4"))
FEE_PER_ROUND = (FEE_BPS_PER_FILL / 10_000) * CAPITAL_PER_LEG * FILLS_PER_ROUND

EXPECTED_HOLD_DAYS = float(os.environ.get("EXPECTED_HOLD_DAYS", str(379.7 / 60 / 24)))


# =============================================================================
# HYPERLIQUID DATA FETCHING
# =============================================================================

def fetch_latest_close(coin: str) -> float:
    now_ms = int(time.time() * 1000)
    lookback_ms = 15 * 60 * 1000 * 3
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": INTERVAL,
                 "startTime": now_ms - lookback_ms, "endTime": now_ms},
    }
    resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
    resp.raise_for_status()
    candles = resp.json()
    if not candles:
        raise RuntimeError(f"No candle data returned for {coin}")
    return float(candles[-1]["c"])


def fetch_funding_rates() -> dict:
    payload = {"type": "metaAndAssetCtxs", "dex": HIP3_DEX}
    resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()

    universe = meta["universe"]
    rates = {}
    for i, asset in enumerate(universe):
        name = asset["name"]
        if name in (LEG_A_SYMBOL, LEG_B_SYMBOL):
            rates[name] = float(asset_ctxs[i]["funding"])

    missing = {LEG_A_SYMBOL, LEG_B_SYMBOL} - rates.keys()
    if missing:
        raise RuntimeError(f"Missing funding rate for: {missing}")
    return rates


# =============================================================================
# SIGNAL LOGIC
# =============================================================================

def compute_zscore() -> dict:
    price_a = fetch_latest_close(LEG_A_SYMBOL)
    price_b = fetch_latest_close(LEG_B_SYMBOL)
    spread = price_a - price_b
    z = (spread - SPREAD_MEAN) / SPREAD_STD if SPREAD_STD > 0 else 0.0
    return {"spread": spread, "z": z, "price_A": price_a, "price_B": price_b}


def suggest_exit_level(z: float) -> dict:
    exit_z = EXIT_Z_THRESHOLD if z > 0 else -EXIT_Z_THRESHOLD
    exit_z = exit_z if exit_z != 0 else 0.0
    exit_spread = SPREAD_MEAN + exit_z * SPREAD_STD
    return {"exit_z": exit_z, "exit_spread": exit_spread}


def estimate_expected_pnl(stats: dict) -> float:
    avg_price = (stats["price_A"] + stats["price_B"]) / 2
    barrels_per_leg = CAPITAL_PER_LEG / max(avg_price, 1)
    deviation = abs(stats["spread"] - SPREAD_MEAN)
    return deviation * barrels_per_leg


def estimate_funding_cost(stats: dict, funding_rates: dict) -> dict:
    daily_rate_a = funding_rates[LEG_A_SYMBOL] * 24
    daily_rate_b = funding_rates[LEG_B_SYMBOL] * 24

    if stats["z"] > 0:
        cost_a = -CAPITAL_PER_LEG * daily_rate_a
        cost_b = CAPITAL_PER_LEG * daily_rate_b
    else:
        cost_a = CAPITAL_PER_LEG * daily_rate_a
        cost_b = -CAPITAL_PER_LEG * daily_rate_b

    daily_funding_cost = cost_a + cost_b
    total_funding_cost = daily_funding_cost * EXPECTED_HOLD_DAYS

    return {
        "daily_rate_a": daily_rate_a, "daily_rate_b": daily_rate_b,
        "daily_funding_cost": daily_funding_cost, "total_funding_cost": total_funding_cost,
    }


def evaluate_signal(force_funding_check: bool = False) -> dict:
    stats = compute_zscore()
    z = stats["z"]

    result = {
        "z": z, "spread": stats["spread"],
        "price_A": stats["price_A"], "price_B": stats["price_B"],
        "should_enter": False, "reason": "z-score dưới ngưỡng",
    }

    if abs(z) < SIGNAL_THRESHOLD and not force_funding_check:
        return result

    funding_rates = fetch_funding_rates()
    funding = estimate_funding_cost(stats, funding_rates)
    expected_pnl = estimate_expected_pnl(stats)
    net_expected = expected_pnl - FEE_PER_ROUND - funding["total_funding_cost"]
    exit_level = suggest_exit_level(z)

    result.update({
        "expected_pnl": expected_pnl, "fee_per_round": FEE_PER_ROUND,
        "funding": funding, "net_expected": net_expected, "exit_level": exit_level,
    })

    if abs(z) >= SIGNAL_THRESHOLD:
        if net_expected > 0:
            result["should_enter"] = True
            result["reason"] = "Lợi nhuận kỳ vọng > phí + funding cost"
        else:
            result["reason"] = "Funding cost + phí ăn hết lợi nhuận kỳ vọng -> bỏ qua"
    else:
        result["reason"] = "z-score dưới ngưỡng (đã tính funding tham khảo)"

    return result


# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram_message(text: str) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def build_check_message(result: dict) -> str:
    z = result["z"]
    status_icon = "✅" if result["should_enter"] else "⏸"

    lines = [
        f"*[CHECK] PAIRS STATUS — CL/BRENTOIL*",
        f"{status_icon} {result['reason']}\n",
        f"Z-score: `{z:.2f}` (ngưỡng {SIGNAL_THRESHOLD})",
        f"Spread hiện tại: `${result['spread']:.3f}/bbl`",
        f"Mean cố định: `${SPREAD_MEAN:.3f}` | Std cố định: `${SPREAD_STD:.3f}`",
        f"Giá xyz:CL: `${result['price_A']:.2f}` | Giá xyz:BRENTOIL: `${result['price_B']:.2f}`",
    ]

    if "net_expected" in result:
        f = result["funding"]
        ex = result["exit_level"]
        lines += [
            "",
            f"Lợi nhuận kỳ vọng: `${result['expected_pnl']:.2f}`",
            f"Phí trade: `${result['fee_per_round']:.2f}`",
            f"Funding/ngày dự kiến: `${f['daily_funding_cost']:.2f}`",
            f"Funding cost ước tính ({EXPECTED_HOLD_DAYS:.2f} ngày): `${f['total_funding_cost']:.2f}`",
            f"*Net kỳ vọng: `${result['net_expected']:.2f}`*",
            "",
            f"🎯 Gợi ý đóng lệnh (nếu đang mở): spread về `${ex['exit_spread']:.3f}/bbl` (z ≈ `{ex['exit_z']:.2f}`)",
        ]

    if result["should_enter"]:
        direction = "🔴 SHORT SPREAD (Short xyz:CL / Long xyz:BRENTOIL)" if z > 0 \
            else "🟢 LONG SPREAD (Long xyz:CL / Short xyz:BRENTOIL)"
        lines += ["", f"→ {direction}"]

    return "\n".join(lines)


def result_to_json(result: dict) -> dict:
    response = {
        "z": round(result["z"], 3), "spread": round(result["spread"], 4),
        "should_enter": result["should_enter"], "reason": result["reason"],
    }
    if "net_expected" in result:
        response["expected_pnl"] = round(result["expected_pnl"], 2)
        response["fee_per_round"] = round(result["fee_per_round"], 2)
        response["daily_funding_cost"] = round(result["funding"]["daily_funding_cost"], 2)
        response["total_funding_cost"] = round(result["funding"]["total_funding_cost"], 2)
        response["net_expected"] = round(result["net_expected"], 2)
        response["suggested_exit_z"] = round(result["exit_level"]["exit_z"], 3)
        response["suggested_exit_spread"] = round(result["exit_level"]["exit_spread"], 4)
    return response


def check_auth(headers) -> bool:
    if not CRON_SECRET:
        return True
    return headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"


# =============================================================================
# HTTP HANDLER (Vercel entrypoint)
# =============================================================================

class handler(BaseHTTPRequestHandler):
    def _handle(self):
        if not check_auth(self.headers):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            result = evaluate_signal(force_funding_check=True)
            tg_response = send_telegram_message(build_check_message(result))
            response = result_to_json(result)

            # --- DEBUG BLOCK: xoá sau khi xác định xong nguyên nhân ---
            response["_debug"] = {
                "chat_id_env_used": TELEGRAM_CHAT_ID,
                "bot_token_last6": TELEGRAM_BOT_TOKEN[-6:] if TELEGRAM_BOT_TOKEN else None,
                "telegram_ok": tg_response.get("ok"),
                "telegram_message_id": tg_response.get("result", {}).get("message_id"),
                "telegram_chat_delivered_to": tg_response.get("result", {}).get("chat", {}),
            }
            # --- END DEBUG BLOCK ---

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response, indent=2).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()