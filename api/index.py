"""
=============================================================================
Endpoint: POST/GET /api/index — PAIRS TRADING SIGNAL BOT (CL vs BRENTOIL)
=============================================================================
Dùng cho cron-job.org — ping mỗi 5 phút, quét tín hiệu và GỬI TELEGRAM khi
đủ điều kiện (should_enter=True).

QUAN TRỌNG: file này KHÔNG import từ file .py nào khác trong repo. Vercel
Python runtime bundle MỖI file trong api/ thành 1 function HOÀN TOÀN ĐỘC LẬP
— import chéo kiểu "from _pairs_bot import ..." sẽ luôn lỗi
"ModuleNotFoundError" dù file kia có tồn tại trong repo hay không. Vì vậy
toàn bộ logic (config, fetch Hyperliquid, tính z-score, funding, Telegram)
được viết thẳng trong file này. File api/check.py có cùng nội dung logic,
lặp lại có chủ đích — đây là đánh đổi bắt buộc để tránh lỗi import.

LOGIC TỔNG QUAN:
    1. Lấy giá hiện tại (nến m15 gần nhất) của 2 leg -> tính spread hiện tại.
    2. So spread với SPREAD_MEAN / SPREAD_STD CỐ ĐỊNH (không tính lại rolling
       mỗi lần chạy) -> ra z-score.
    3. Nếu |z| < SIGNAL_THRESHOLD -> should_enter = False, dừng ở đây.
    4. Nếu |z| >= SIGNAL_THRESHOLD -> tính lợi nhuận kỳ vọng nếu spread hồi
       mean, tính funding cost dự kiến, trừ phí trade. should_enter = True
       chỉ khi lợi nhuận kỳ vọng > phí trade + funding cost.

ENV VARS (Project Settings -> Environment Variables trên Vercel):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET,
    SPREAD_MEAN, SPREAD_STD, SIGNAL_THRESHOLD, EXIT_Z_THRESHOLD,
    EXPECTED_HOLD_DAYS, CAPITAL_PER_LEG, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    (tất cả có default hợp lý trong code, không set vẫn chạy được)
=============================================================================
"""

import os
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

# =============================================================================
# CONFIG
# =============================================================================

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LEG_A_SYMBOL = "xyz:CL"          # WTI perp
LEG_B_SYMBOL = "xyz:BRENTOIL"    # Brent perp
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
    # Lấy giá 2 leg song song (thread pool) thay vì tuần tự — giảm thời gian
    # chờ từ "8s + 8s" xuống còn ~8s (thời gian của lệnh chậm nhất), giúp
    # /api/index ít có nguy cơ chạm giới hạn duration của Vercel hơn nữa.
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(fetch_latest_close, LEG_A_SYMBOL)
        fut_b = ex.submit(fetch_latest_close, LEG_B_SYMBOL)
        price_a = fut_a.result()
        price_b = fut_b.result()
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

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()


def build_signal_message(result: dict) -> str:
    z = result["z"]
    direction = "🔴 SHORT SPREAD (Short xyz:CL / Long xyz:BRENTOIL)" if z > 0 \
        else "🟢 LONG SPREAD (Long xyz:CL / Short xyz:BRENTOIL)"

    f = result["funding"]
    ex = result["exit_level"]
    return (
        f"*PAIRS SIGNAL — CL/BRENTOIL*\n"
        f"{direction}\n\n"
        f"Z-score: `{z:.2f}` (ngưỡng {SIGNAL_THRESHOLD})\n"
        f"Spread hiện tại: `${result['spread']:.3f}/bbl`\n"
        f"Mean cố định: `${SPREAD_MEAN:.3f}` | Std cố định: `${SPREAD_STD:.3f}`\n\n"
        f"Lợi nhuận kỳ vọng (nếu hồi mean): `${result['expected_pnl']:.2f}`\n"
        f"Phí trade ({FILLS_PER_ROUND} fill x {FEE_BPS_PER_FILL}bps): `${result['fee_per_round']:.2f}`\n"
        f"Funding/ngày dự kiến: `${f['daily_funding_cost']:.2f}`\n"
        f"Funding cost ước tính ({EXPECTED_HOLD_DAYS:.2f} ngày hold): `${f['total_funding_cost']:.2f}`\n"
        f"*Net kỳ vọng: `${result['net_expected']:.2f}`*\n\n"
        f"Giá xyz:CL: `${result['price_A']:.2f}` | Giá xyz:BRENTOIL: `${result['price_B']:.2f}`\n\n"
        f"🎯 *Gợi ý đóng lệnh*: khi spread về lại `${ex['exit_spread']:.3f}/bbl` "
        f"(z ≈ `{ex['exit_z']:.2f}`)\n"
        f"_Bot không tự động báo khi tới điểm đóng — bạn tự theo dõi bằng /check, "
        f"hoặc đặt take-profit/limit tương ứng ngay khi vào lệnh._"
    )


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
            result = evaluate_signal()

            if result["should_enter"]:
                send_telegram_message(build_signal_message(result))

            response = result_to_json(result)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()