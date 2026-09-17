"""
=============================================================================
Pairs Trading Signal Bot — HỖ TRỢ NHIỀU CẶP (multi-pair)
=============================================================================
1 FILE Flask app duy nhất, 2 ROUTE nội bộ — giữ đúng kiến trúc gốc đã chạy
ổn định (không tự đoán nguồn request từ header, Flask nhận đúng request.path
thật /api hay /api/webhook nhờ vercel.json rewrites).

    GET/POST /api          -> cron-job.org ping mỗi 5 phút, quét TẤT CẢ các
                               cặp trong PAIRS, gửi Telegram trạng thái mỗi lần
                               quét (không cần đủ ngưỡng tín hiệu).
    POST     /api/webhook  -> Telegram tự gọi mỗi khi có tin nhắn mới.
                               /check            -> trạng thái TẤT CẢ cặp
                               /check <pair_id>  -> trạng thái 1 cặp cụ thể
                               /entry            -> gợi ý vào lệnh
                               (pair_id: "cl" / "xyz100" / "goldsilver")

CẶP ĐANG THEO DÕI:
    1. cl         — xyz:CL vs xyz:BRENTOIL        — spread = price_A - price_B ($/bbl)
                    nguồn: Hyperliquid HIP-3 (xyz)
                    mean/std từ nến 15m ~52 ngày (2026-07-22 → 2026-09-12)
    2. xyz100     — xyz:XYZ100 vs xyz:SP500       — spread = ln(price_A / price_B)
                    nguồn: Hyperliquid HIP-3 (xyz)
    3. goldsilver — xyz:GOLD vs xyz:SILVER        — spread = ln(price_A / price_B)
                    nguồn: Hyperliquid HIP-3 (xyz)

QUAN TRỌNG VỀ VERCEL ROUTING: xem vercel.json — bắt buộc có "rewrites" trỏ
"/api" và "/api/webhook" về "/api/index", nếu không sẽ bị 404 ở tầng Vercel.

ENV VARS (Project Settings -> Environment Variables trên Vercel):
    Chung: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET,
           TELEGRAM_WEBHOOK_SECRET, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    Theo từng cặp (suffix _CL, _XYZ100, _GOLDSILVER), tất cả có default hợp lý:
           SPREAD_MEAN_<X>, SPREAD_STD_<X>, SIGNAL_THRESHOLD_<X>,
           EXIT_Z_THRESHOLD_<X>, EXPECTED_HOLD_DAYS_<X>, CAPITAL_PER_LEG_<X>
=============================================================================
"""

import os
import math
import time
from datetime import datetime, timezone, timedelta
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify

app = Flask(__name__)

# =============================================================================
# CONFIG CHUNG
# =============================================================================

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVAL = "15m"
HIP3_DEX = "xyz"

# Variational Omni — public read-only API (không có nến lịch sử, không cần auth)
VAR_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
_VAR_LISTINGS_CACHE = {"ts": 0.0, "listings": None}
_VAR_CACHE_TTL_S = 8.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

FEE_BPS_PER_FILL = float(os.environ.get("FEE_BPS_PER_FILL", "2.2"))
FILLS_PER_ROUND = int(os.environ.get("FILLS_PER_ROUND", "4"))


# =============================================================================
# CẤU HÌNH TỪNG CẶP (PAIRS)
# =============================================================================

def _pair_env(key: str, suffix: str, default: str) -> str:
    return os.environ.get(f"{key}_{suffix}", default)


PAIRS = [
    {
        "id": "cl",
        "label": "CL/BRENTOIL",
        "venue": "hyperliquid",
        "symbol_a": "xyz:CL",
        "symbol_b": "xyz:BRENTOIL",
        "spread_type": "diff",
        # Hyperliquid 1H 90 ngày: 2026-06-19 → 2026-09-17, 2164 nến
        # mean -4.1789 / std 0.8487 / min -6.009 / max -1.897
        # Mid range tối ưu: 1.3σ ≈ $1.10 (5 lệnh đóng, net $357, $71/lệnh)
        # Full range: 2.0σ ≈ $1.70 (gần biên min/max)
        "mean": float(_pair_env("SPREAD_MEAN", "CL", "-4.1789")),
        "std": float(_pair_env("SPREAD_STD", "CL", "0.8487")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "CL", "1.3")),
        "mid_z": float(_pair_env("MID_Z", "CL", "1.3")),
        "full_z": float(_pair_env("FULL_Z", "CL", "2.0")),
        "range_min": float(_pair_env("RANGE_MIN", "CL", "-6.009")),
        "range_max": float(_pair_env("RANGE_MAX", "CL", "-1.897")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "CL", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "CL", str(270.0 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "CL", "5000")),
    },
    {
        "id": "xyz100",
        "label": "XYZ100/SP500",
        "venue": "hyperliquid",
        "symbol_a": "xyz:XYZ100",
        "symbol_b": "xyz:SP500",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "XYZ100", "1.3805")),
        "std": float(_pair_env("SPREAD_STD", "XYZ100", "0.0118")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "XYZ100", "2.25")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "XYZ100", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "XYZ100", str(56 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "XYZ100", "5000")),
    },
    {
        "id": "goldsilver",
        "label": "GOLD/SILVER",
        "venue": "hyperliquid",
        "symbol_a": "xyz:GOLD",
        "symbol_b": "xyz:SILVER",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "GOLDSILVER", "4.23056")),
        "std": float(_pair_env("SPREAD_STD", "GOLDSILVER", "0.020552")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "GOLDSILVER", "2")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "GOLDSILVER", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "GOLDSILVER", str(798 / 60 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "GOLDSILVER", "5000")),
    },
    # xau tạm tắt
]

PAIRS_BY_ID = {p["id"]: p for p in PAIRS}


def fee_per_round(pair: dict) -> float:
    return (FEE_BPS_PER_FILL / 10_000) * pair["capital_per_leg"] * FILLS_PER_ROUND


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


def fetch_funding_rates(symbol_a: str, symbol_b: str) -> dict:
    payload = {"type": "metaAndAssetCtxs", "dex": HIP3_DEX}
    resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()

    universe = meta["universe"]
    rates = {}
    for i, asset in enumerate(universe):
        name = asset["name"]
        if name in (symbol_a, symbol_b):
            rates[name] = float(asset_ctxs[i]["funding"])

    missing = {symbol_a, symbol_b} - rates.keys()
    if missing:
        raise RuntimeError(f"Missing funding rate for: {missing}")
    return rates


# =============================================================================
# VARIATIONAL (giữ sẵn nếu bật lại cặp xau)
# =============================================================================

def fetch_variational_listings() -> list:
    now = time.time()
    cached = _VAR_LISTINGS_CACHE["listings"]
    if cached is not None and (now - _VAR_LISTINGS_CACHE["ts"]) < _VAR_CACHE_TTL_S:
        return cached
    resp = requests.get(VAR_STATS_URL, timeout=8)
    resp.raise_for_status()
    listings = resp.json().get("listings") or []
    if not listings:
        raise RuntimeError("Variational /metadata/stats returned empty listings")
    _VAR_LISTINGS_CACHE["ts"] = now
    _VAR_LISTINGS_CACHE["listings"] = listings
    return listings


def _variational_hourly_decimal(listing: dict) -> float:
    raw = float(listing.get("funding_rate") or 0.0)
    return raw / 8760.0


def fetch_variational_pair(symbol_a: str, symbol_b: str) -> dict:
    listings = fetch_variational_listings()
    by_ticker = {str(x.get("ticker")): x for x in listings}
    missing = [s for s in (symbol_a, symbol_b) if s not in by_ticker]
    if missing:
        raise RuntimeError(f"Variational listings missing ticker(s): {missing}")
    la, lb = by_ticker[symbol_a], by_ticker[symbol_b]
    return {
        "price_a": float(la["mark_price"]),
        "price_b": float(lb["mark_price"]),
        "funding_rates": {
            symbol_a: _variational_hourly_decimal(la),
            symbol_b: _variational_hourly_decimal(lb),
        },
    }


def compute_spread(price_a: float, price_b: float, spread_type: str) -> float:
    if spread_type == "logratio":
        return math.log(price_a / price_b)
    return price_a - price_b


def compute_zscore(pair: dict, with_funding: bool) -> dict:
    symbol_a, symbol_b = pair["symbol_a"], pair["symbol_b"]
    venue = pair.get("venue", "hyperliquid")

    if venue == "variational":
        var = fetch_variational_pair(symbol_a, symbol_b)
        price_a, price_b = var["price_a"], var["price_b"]
        funding_rates = var["funding_rates"] if with_funding else None
    else:
        with ThreadPoolExecutor(max_workers=3) as ex:
            fut_a = ex.submit(fetch_latest_close, symbol_a)
            fut_b = ex.submit(fetch_latest_close, symbol_b)
            fut_funding = (
                ex.submit(fetch_funding_rates, symbol_a, symbol_b) if with_funding else None
            )
            price_a = fut_a.result()
            price_b = fut_b.result()
            funding_rates = fut_funding.result() if fut_funding else None

    spread = compute_spread(price_a, price_b, pair["spread_type"])
    std = pair["std"]
    z = (spread - pair["mean"]) / std if std > 0 else 0.0
    return {
        "spread": spread, "z": z, "price_A": price_a, "price_B": price_b,
        "funding_rates": funding_rates,
    }


# =============================================================================
# SIGNAL LOGIC
# =============================================================================

def suggest_exit_level(pair: dict, z: float) -> dict:
    exit_z = pair["exit_z"] if z > 0 else -pair["exit_z"]
    exit_z = exit_z if exit_z != 0 else 0.0
    exit_spread = pair["mean"] + exit_z * pair["std"]
    return {"exit_z": exit_z, "exit_spread": exit_spread}


def estimate_expected_pnl(pair: dict, stats: dict) -> float:
    deviation = abs(stats["spread"] - pair["mean"])
    if pair["spread_type"] == "logratio":
        return deviation * pair["capital_per_leg"]
    avg_price = (stats["price_A"] + stats["price_B"]) / 2
    units_per_leg = pair["capital_per_leg"] / max(avg_price, 1)
    return deviation * units_per_leg


def estimate_funding_cost(pair: dict, stats: dict, funding_rates: dict) -> dict:
    symbol_a, symbol_b = pair["symbol_a"], pair["symbol_b"]
    daily_rate_a = funding_rates[symbol_a] * 24
    daily_rate_b = funding_rates[symbol_b] * 24
    capital = pair["capital_per_leg"]

    if stats["z"] > 0:
        cost_a = -capital * daily_rate_a
        cost_b = capital * daily_rate_b
    else:
        cost_a = capital * daily_rate_a
        cost_b = -capital * daily_rate_b

    daily_funding_cost = cost_a + cost_b
    total_funding_cost = daily_funding_cost * pair["expected_hold_days"]

    return {
        "daily_rate_a": daily_rate_a, "daily_rate_b": daily_rate_b,
        "daily_funding_cost": daily_funding_cost, "total_funding_cost": total_funding_cost,
    }


def evaluate_signal(pair: dict, force_funding_check: bool = False) -> dict:
    stats = compute_zscore(pair, with_funding=force_funding_check)
    z = stats["z"]

    result = {
        "pair_id": pair["id"], "pair_label": pair["label"],
        "z": z, "spread": stats["spread"],
        "price_A": stats["price_A"], "price_B": stats["price_B"],
        "should_enter": False, "reason": "z-score dưới ngưỡng",
    }

    if abs(z) < pair["threshold"] and not force_funding_check:
        return result

    if stats["funding_rates"] is not None:
        funding_rates = stats["funding_rates"]
    elif pair.get("venue") == "variational":
        funding_rates = fetch_variational_pair(pair["symbol_a"], pair["symbol_b"])["funding_rates"]
    else:
        funding_rates = fetch_funding_rates(pair["symbol_a"], pair["symbol_b"])
    funding = estimate_funding_cost(pair, stats, funding_rates)
    expected_pnl = estimate_expected_pnl(pair, stats)
    fee = fee_per_round(pair)
    net_expected = expected_pnl - fee - funding["total_funding_cost"]
    exit_level = suggest_exit_level(pair, z)

    result.update({
        "funding": funding,
        "expected_pnl": expected_pnl,
        "fee_per_round": fee,
        "net_expected": net_expected,
        "exit_level": exit_level,
    })

    if abs(z) >= pair["threshold"] and net_expected > 0:
        result["should_enter"] = True
        result["reason"] = "Đủ điều kiện vào lệnh (net kỳ vọng > 0)"
    elif abs(z) >= pair["threshold"]:
        result["reason"] = "Z-score đủ ngưỡng nhưng net kỳ vọng <= 0 (phí+funding ăn hết lợi nhuận)"
    else:
        result["reason"] = "z-score dưới ngưỡng (đã tính funding tham khảo)"

    return result


# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram_message(text: str, chat_id: str = None):
    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat_id:
        print(f"[TG] Missing token/chat_id, would have sent: {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": target_chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[ERROR] Gửi Telegram thất bại: {e}")


def _direction_text(pair: dict, z: float) -> str:
    if z > 0:
        return f"🔴 SHORT {pair['symbol_a']} / LONG {pair['symbol_b']}"
    return f"🟢 LONG {pair['symbol_a']} / SHORT {pair['symbol_b']}"


def classify_range_zone(pair: dict, result: dict):
    """MID nếu |z| >= mid_z; FULL nếu |z| >= full_z hoặc chạm min/max 90 ngày."""
    mid_z = float(pair.get("mid_z", pair.get("threshold", 1.3)))
    full_z = float(pair.get("full_z", 2.0))
    az = abs(result["z"])
    sp = result["spread"]
    rmin = pair.get("range_min")
    rmax = pair.get("range_max")
    at_extreme = False
    if rmin is not None and rmax is not None:
        # trong 0.15$/bbl so với biên quan sát
        at_extreme = sp <= float(rmin) + 0.15 or sp >= float(rmax) - 0.15
    if az >= full_z or at_extreme:
        return "FULL"
    if az >= mid_z:
        return "MID"
    return None


def build_status_message(pair: dict, result: dict) -> str:
    """Luôn báo đủ trạng thái. CL: WAIT / MID / FULL. Cặp khác: vào hoặc chưa nên vào."""
    z = result["z"]
    net = result.get("net_expected")
    net_txt = f"${net:.2f}" if net is not None else "n/a"
    exit_spread = (result.get("exit_level") or {}).get("exit_spread", pair["mean"])
    prices = (
        f"Spread: `{result['spread']:.4f}` (z `{z:.2f}`)\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | "
        f"Giá {pair['symbol_b']}: `${result['price_B']:.2f}`"
    )

    if pair.get("id") == "cl":
        zone = classify_range_zone(pair, result)
        mid_z = float(pair.get("mid_z", 1.3))
        full_z = float(pair.get("full_z", 2.0))
        sign = 1 if z > 0 else -1
        mid_lvl = pair["mean"] + mid_z * pair["std"] * sign
        full_lvl = pair["mean"] + full_z * pair["std"] * sign
        hold_h = pair["expected_hold_days"] * 24
        if zone == "FULL":
            hold_h = max(hold_h, 292.0)
        out_dt = datetime.now(timezone.utc) + timedelta(hours=hold_h)
        if zone == "FULL":
            action = "FULL RANGE — VÀO LỆNH"
        elif zone == "MID":
            action = "MID RANGE — VÀO LỆNH"
        else:
            action = f"CHƯA NÊN VÀO"
        return (
            "--------------------------------\n\n"
            f"*CL/BRENT — {action}*\n"
            f"{_direction_text(pair, z)}\n\n"
            f"{prices}\n"
            f"Mean `{pair['mean']:.4f}` | Mid `{mid_lvl:.3f}` | Full `{full_lvl:.3f}`\n\n"
            f"*Net PnL nếu vào giờ → về mean: `{net_txt}`*\n"
            f"Đóng khi spread về `{exit_spread:.4f}`\n"
            f"Hold TB ~{hold_h:.0f}h\n\n"
            f"Gõ /check để xem giá hiện tại và /entry để biết gợi ý vào lệnh"
        )

    can_enter = abs(z) >= pair.get("threshold", 99)
    action = "VÀO LỆNH" if can_enter else "CHƯA NÊN VÀO"
    return (
        "--------------------------------\n\n"
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{action}\n"
        f"{_direction_text(pair, z)}\n\n"
        f"{prices}\n\n"
        f"*Bú Net PnL: `{net_txt}`*\n\n"
        f"Gõ /entry để biết gợi ý vào lệnh"
    )


def build_signal_message(pair: dict, result: dict) -> str:
    return (
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{_direction_text(pair, result['z'])}\n\n"
        f"Spread: `{result['spread']:.4f}`\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | "
        f"Giá {pair['symbol_b']}: `${result['price_B']:.2f}`\n\n"
        f"*Bú Net PnL: `${result['net_expected']:.2f}`*\n\n"
        f"Gõ /check để biết giá hiện tại và /entry để biết gợi ý vào lệnh"
    )


def build_check_message(pair: dict, result: dict) -> str:
    net = result.get("net_expected")
    net_txt = f"${net:.2f}" if net is not None else "n/a"
    return (
        "--------------------------------\n\n"
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{_direction_text(pair, result['z'])}\n\n"
        f"Spread: `{result['spread']:.4f}`\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | "
        f"Giá {pair['symbol_b']}: `${result['price_B']:.2f}`\n\n"
        f"*Bú Net PnL: `{net_txt}`*\n\n"
        f"Gõ /entry để biết gợi ý vào lệnh"
    )


HELP_TEXT = (
    "*PAIRS BOT — MULTI-PAIR*\n"
    "Đang theo dõi 3 cặp:\n"
    "• `cl` — CL/BRENTOIL (WTI vs Brent) — Hyperliquid\n"
    "• `xyz100` — XYZ100/SP500\n"
    "• `goldsilver` — GOLD/SILVER\n\n"
    "Gõ /check để xem trạng thái TẤT CẢ cặp ngay lúc này.\n"
    "Gõ /check cl, /check xyz100 hoặc /check goldsilver để xem riêng 1 cặp.\n"
    "Gõ /entry để xem gợi ý vào lệnh.\n"
    "Cron gửi trạng thái tất cả cặp mỗi lần quét, không cần đủ ngưỡng."
)

PAIRS_TEXT = (
    "*GỢI Ý VÀO LỆNH*\n\n"
    "🟢 LONG BRENTOIL / SHORT CL khi Net PnL <= 60 \n"
    "🔴 SHORT BRENTOIL / LONG CL khi Net PnL >= 90 \n\n"
    "Chia vốn thành 4-5 phần, cứ 10 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *30 đến 150*, chỉ vào lệnh khi Net PnL <= 60 hoặc >= 90.\n"
    "--------------------------------\n"
    "🟢 LONG QQQ / SHORT US500 khi Net PnL >= 190 \n"
    "🔴 SHORT QQQ / LONG US500 khi Net PnL <= 160 \n\n"
    "Chia vốn thành 4-5 phần, cứ 20 - 30 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *120 đến 350*, chỉ vào lệnh khi Net PnL >= 190 hoặc <= 160.\n"
    "--------------------------------\n"
    "🟢 LONG GOLD / SHORT SILVER khi Net PnL >= 150 \n"
    "🔴 SHORT GOLD / LONG SILVER khi Net PnL <= 50 \n\n"
    "Chia vốn thành 4-5 phần, cứ 35 - 45 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *20 đến 200*, chỉ vào lệnh khi Net PnL <= 50 hoặc >= 150.\n\n\n"
    "*Giải thích*:\n"
    "2k/leg: 2k long và 2k short\n"
    "Net PnL: Lợi nhuận ròng đang tính với vol 5k/leg\n\n\n"
    "*LUÔN KỶ LUẬT KHI VÀO LỆNH*"
)


def result_to_json(result: dict) -> dict:
    response = {
        "pair_id": result["pair_id"], "pair_label": result["pair_label"],
        "z": round(result["z"], 4), "spread": round(result["spread"], 4),
        "should_enter": result["should_enter"], "reason": result["reason"],
    }
    if "net_expected" in result:
        response["expected_pnl"] = round(result["expected_pnl"], 2)
        response["fee_per_round"] = round(result["fee_per_round"], 2)
        response["daily_funding_cost"] = round(result["funding"]["daily_funding_cost"], 2)
        response["total_funding_cost"] = round(result["funding"]["total_funding_cost"], 2)
        response["net_expected"] = round(result["net_expected"], 2)
        response["suggested_exit_z"] = round(result["exit_level"]["exit_z"], 4)
        response["suggested_exit_spread"] = round(result["exit_level"]["exit_spread"], 4)
    return response


def check_cron_auth() -> bool:
    if not CRON_SECRET:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"


def check_telegram_secret() -> bool:
    if not TELEGRAM_WEBHOOK_SECRET:
        return True
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") == TELEGRAM_WEBHOOK_SECRET


# =============================================================================
# ROUTE 1: /api — cron gửi Telegram mỗi lần quét
# =============================================================================

@app.route("/", methods=["GET", "POST"])
@app.route("/api", methods=["GET", "POST"])
@app.route("/api/", methods=["GET", "POST"])
@app.route("/api/index", methods=["GET", "POST"])
def scan_bot():
    if not check_cron_auth():
        return jsonify({"error": "Unauthorized"}), 401

    results = {}
    errors = {}
    sections = []
    for pair in PAIRS:
        try:
            result = evaluate_signal(pair, force_funding_check=True)
            results[pair["id"]] = result_to_json(result)
            sections.append(build_status_message(pair, result))
        except Exception as e:
            print(f"[ERROR] scan_bot pair={pair['id']}: {e}")
            errors[pair["id"]] = str(e)
            sections.append(f"*{pair['label']}*\n❌ Lỗi: `{e}`")

    if sections:
        send_telegram_message("*[SCAN]*\n\n" + "\n\n".join(sections))

    status_code = 200 if not errors or results else 500
    return jsonify({"results": results, "errors": errors}), status_code


# =============================================================================
# ROUTE 2: /api/webhook
# =============================================================================

@app.route("/api/webhook", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def telegram_webhook():
    if not check_telegram_secret():
        return jsonify({"ok": True}), 200

    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": True}), 200

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True}), 200

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return jsonify({"ok": True}), 200

    parts = text.split()
    command = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1].lower() if len(parts) > 1 else None

    try:
        if command in ("/start", "/help"):
            send_telegram_message(HELP_TEXT, chat_id=chat_id)
        elif command == "/entry":
            send_telegram_message(PAIRS_TEXT, chat_id=chat_id)
        elif command == "/check":
            if arg and arg in PAIRS_BY_ID:
                pair = PAIRS_BY_ID[arg]
                result = evaluate_signal(pair, force_funding_check=True)
                msg = f"*[CHECK] {pair['label']}*\n\n" + build_status_message(pair, result)
                send_telegram_message(msg, chat_id=chat_id)
            elif arg:
                send_telegram_message(
                    f"Không tìm thấy cặp `{arg}`. Các cặp hợp lệ: "
                    + ", ".join(f"`{pid}`" for pid in PAIRS_BY_ID),
                    chat_id=chat_id,
                )
            else:
                sections = []
                for pair in PAIRS:
                    result = evaluate_signal(pair, force_funding_check=True)
                    sections.append(build_status_message(pair, result))
                msg = "*[CHECK] PAIRS STATUS*\n\n" + "\n\n".join(sections)
                send_telegram_message(msg, chat_id=chat_id)
        elif command:
            send_telegram_message(
                "Lệnh không hợp lệ. Gõ /check, /check <cl|xyz100|goldsilver> hoặc /entry.",
                chat_id=chat_id,
            )
    except Exception as e:
        send_telegram_message(f"❌ Lỗi: {e}", chat_id=chat_id)

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)