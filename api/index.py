"""
=============================================================================
Pairs Trading Signal Bot — HỖ TRỢ NHIỀU CẶP (multi-pair)
=============================================================================
1 FILE Flask app duy nhất, 2 ROUTE nội bộ — giữ đúng kiến trúc gốc đã chạy
ổn định (không tự đoán nguồn request từ header, Flask nhận đúng request.path
thật /api hay /api/webhook nhờ vercel.json rewrites).

    GET/POST /api          -> cron-job.org ping mỗi 5 phút, quét TẤT CẢ các
                               cặp trong PAIRS, gửi Telegram riêng cho MỖI
                               cặp đủ điều kiện vào lệnh.
    POST     /api/webhook  -> Telegram tự gọi mỗi khi có tin nhắn mới.
                               /check            -> trạng thái TẤT CẢ cặp
                               /check <pair_id>  -> trạng thái 1 cặp cụ thể
                               (pair_id: "cl" / "xyz100" / "goldsilver" / "xau")

CẶP ĐANG THEO DÕI:
    1. cl         — xyz:CL vs xyz:BRENTOIL        — spread = price_A - price_B ($/bbl)
                    nguồn: Hyperliquid HIP-3 (xyz)
    2. xyz100     — xyz:XYZ100 vs xyz:SP500       — spread = ln(price_A / price_B)
                    nguồn: Hyperliquid HIP-3 (xyz)
    3. goldsilver — xyz:GOLD vs xyz:SILVER        — spread = ln(price_A / price_B)
                    nguồn: Hyperliquid HIP-3 (xyz)
    4. xau        — Variational XAU vs XAUT       — spread = price_A - price_B ($/oz)
                    XAU = gold spot perp, XAUT = Tether Gold perp.
                    nguồn: Variational GET /metadata/stats (mark + funding).
                    Variational không public nến lịch sử.

QUAN TRỌNG VỀ VERCEL ROUTING: xem vercel.json — bắt buộc có "rewrites" trỏ
"/api" và "/api/webhook" về "/api/index", nếu không sẽ bị 404 ở tầng Vercel.

ENV VARS (Project Settings -> Environment Variables trên Vercel):
    Chung: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET,
           TELEGRAM_WEBHOOK_SECRET, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    Theo từng cặp (suffix _CL, _XYZ100, _GOLDSILVER hoặc _XAU), tất cả có default hợp lý:
           SPREAD_MEAN_<X>, SPREAD_STD_<X>, SIGNAL_THRESHOLD_<X>,
           EXIT_Z_THRESHOLD_<X>, EXPECTED_HOLD_DAYS_<X>, CAPITAL_PER_LEG_<X>

⚠️ CẶP xyz100/SP500 MỚI, MẪU BACKTEST NHỎ (9 trades, ~53 ngày data, CHƯA
   cộng funding rate lịch sử vào PnL backtest) — xem README mục cảnh báo.
⚠️ CẶP goldsilver: backtest 90 ngày cho thấy chỉ NGƯỠNG >= 2.5 mới có lãi
   ròng (ngưỡng thấp hơn lỗ vì spread rất "chặt" -> phí ăn hết lợi nhuận).
   Mặc định threshold=3.0 nhưng mẫu chỉ 29 trades/90 ngày -> theo dõi sát
   trước khi tăng size, xem README mục "Thêm cặp Gold/Silver".
⚠️ CẶP xau (XAU/XAUT trên Variational): mean/std từ proxy OKX 1H 60 ngày
   (PAXG≈XAU, XAUT), KHÔNG phải mark Variational lịch sử. Mặc định
   threshold=1.0σ. Funding Variational interval 4h — xem ghi chú đơn vị
   trong fetch_variational_pair(). Verify số funding/ngày với UI Omni.
=============================================================================
"""

import os
import math
import time
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
_VAR_CACHE_TTL_S = 8.0  # 1 lần gọi /metadata/stats dùng chung 2 leg trong cùng request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

FEE_BPS_PER_FILL = float(os.environ.get("FEE_BPS_PER_FILL", "2.2"))
FILLS_PER_ROUND = int(os.environ.get("FILLS_PER_ROUND", "4"))


# =============================================================================
# CẤU HÌNH TỪNG CẶP (PAIRS) hehe
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
        "spread_type": "diff",              # spread = price_A - price_B
        "mean": float(_pair_env("SPREAD_MEAN", "CL", "-3.2858")),
        "std": float(_pair_env("SPREAD_STD", "CL", "0.4675")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "CL", "1.5")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "CL", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "CL", str(379.7 / 60 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "CL", "5000")),
    },
    {
        "id": "xyz100",
        "label": "XYZ100/SP500",
        "venue": "hyperliquid",
        "symbol_a": "xyz:XYZ100",
        "symbol_b": "xyz:SP500",
        "spread_type": "logratio",          # spread = ln(price_A / price_B)
        "mean": float(_pair_env("SPREAD_MEAN", "XYZ100", "1.3805")),
        "std": float(_pair_env("SPREAD_STD", "XYZ100", "0.0118")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "XYZ100", "2.25")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "XYZ100", "0.0")),
        # hold trung bình backtest ~45-67h -> lấy điểm giữa ~56h
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "XYZ100", str(56 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "XYZ100", "5000")),
    },
    {
        "id": "goldsilver",
        "label": "GOLD/SILVER",
        "venue": "hyperliquid",
        "symbol_a": "xyz:GOLD",
        "symbol_b": "xyz:SILVER",
        "spread_type": "logratio",          # spread = ln(price_A / price_B)
        # Task 2, 90 ngày data (m15) -> spread_stats_table_goldsilver.md
        "mean": float(_pair_env("SPREAD_MEAN", "GOLDSILVER", "4.23056")),
        "std": float(_pair_env("SPREAD_STD", "GOLDSILVER", "0.020552")),
        # Task 3 -> backtest_results_table_goldsilver.md. Ngưỡng 2.5-3.5 mới
        # có lãi (1.0-2.0 lỗ ròng, phí ăn hết vì std spread rất nhỏ -> z dao
        # động nhanh, trade quá dày). Chọn 3.0: net PnL $163.08, net/trade
        # $5.62, 29 trades/90 ngày -> mẫu tạm đủ tin cậy. Ngưỡng 3.5 net/trade
        # cao hơn ($9.92) nhưng chỉ 19 trades -> mẫu quá mỏng để ưu tiên mặc định.
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "GOLDSILVER", "2.5")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "GOLDSILVER", "0.0")),
        # Task 3, hold trung bình threshold=2.5: 798 phút -> 798/60/24 ngày
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "GOLDSILVER", str(798 / 60 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "GOLDSILVER", "5000")),
    },
    {
        "id": "xau",
        "label": "XAUT/XAU",
        "venue": "variational",
        "symbol_a": "XAUT",                  # Variational ticker (gold spot perp)
        "symbol_b": "XAU",                 # Variational ticker (Tether Gold perp)
        "spread_type": "diff",              # spread = XAUT - XAU ($/oz), cùng scale
        # Proxy 1H 60 ngày OKX (PAXG≈XAU, XAUT), 1441 nến:
        # mean 6.5758 / std 6.1410 / p10 -1.80 / p90 15.30
        "mean": float(_pair_env("SPREAD_MEAN", "XAU", "6.5758")),
        "std": float(_pair_env("SPREAD_STD", "XAU", "6.1410")),
        # Expanding-mean robustness: 1.0σ net còn dương, WR cao; 0.5σ phí ăn hết.
        # 1.25σ ít trade hơn nhưng net/trade tốt hơn. Default 1.0.
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "XAU", "1.0")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "XAU", "0.0")),
        # Hold TB ~73.5h @ 1.0σ (expanding mean)
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "XAU", str(73.5 / 24))),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "XAU", "5000")),
    },
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
# VARIATIONAL DATA FETCHING
# Public API chỉ có GET /metadata/stats — đủ mark + funding realtime.
# =============================================================================

def fetch_variational_listings() -> list:
    """Cache ngắn để 2 leg của cùng 1 cặp không gọi API 2 lần."""
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
    """
    Đổi funding Variational về cùng đơn vị Hyperliquid: decimal / giờ
    (để estimate_funding_cost dùng daily_rate = hourly * 24).

    Đã verify 2026-09-03:
      API `funding_rate` là APR dạng decimal — KHÔNG phải %/kỳ.
      0.1095 == 10.95% APR == 0.00125%/giờ (interest mặc định).
      329/550 market đang đúng 0.1095.
      Funding map: "10.95%  4hr: 0.0050%" = 0.1095 * (4/8760)*100.
      DefiLlama "0.0013%" của XAU ≈ % mỗi kỳ 4h, không phải field API.

    hourly_decimal = APR / 8760 = funding_rate / 8760
    payment mỗi kỳ ≈ notional * funding_rate * (interval_s / (365*24*3600))
    """
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
    """Lấy giá 2 leg (và funding, nếu cần) SONG SONG qua thread pool -> giảm
    thời gian chờ tối đa, tránh timeout function trên Vercel."""
    symbol_a, symbol_b = pair["symbol_a"], pair["symbol_b"]
    venue = pair.get("venue", "hyperliquid")

    if venue == "variational":
        # 1 call /metadata/stats lấy cả 2 mark + 2 funding
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
# SIGNAL LOGIC (generic theo pair config)
# =============================================================================

def suggest_exit_level(pair: dict, z: float) -> dict:
    exit_z = pair["exit_z"] if z > 0 else -pair["exit_z"]
    exit_z = exit_z if exit_z != 0 else 0.0
    exit_spread = pair["mean"] + exit_z * pair["std"]
    return {"exit_z": exit_z, "exit_spread": exit_spread}


def estimate_expected_pnl(pair: dict, stats: dict) -> float:
    deviation = abs(stats["spread"] - pair["mean"])
    if pair["spread_type"] == "logratio":
        # Dollar-neutral: PnL ≈ notional_per_leg * Δ(ln(A/B)) khi hedge đúng
        # tỷ trọng $ (không phải số lượng barrel/hợp đồng).
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
    """
    force_funding_check=False (nhánh cron /api): nếu |z| chưa tới ngưỡng,
    dừng ngay sau khi lấy giá — không tốn thêm API call funding.
    force_funding_check=True (nhánh Telegram /check): luôn lấy funding ngay
    từ đầu (chạy song song cùng giá) để trả lời đầy đủ mỗi lần được hỏi.
    """
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
    return (f"🔴 SHORT SPREAD (Short {pair['symbol_a']} / Long {pair['symbol_b']})" if z > 0
            else f"🟢 LONG SPREAD (Long {pair['symbol_a']} / Short {pair['symbol_b']})")


def build_signal_message(pair: dict, result: dict) -> str:
    """Tin nhắn chủ động khi cron phát hiện đủ điều kiện vào lệnh."""
    z = result["z"]
    ex = result["exit_level"]
    return (
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{_direction_text(pair, z)}\n\n"
        f"Z-score: `{z:.2f}` (ngưỡng {pair['threshold']})\n"
        f"Spread hiện tại: `{result['spread']:.4f}`\n"
        f"*Net kỳ vọng: `${result['net_expected']:.2f}`*\n\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | "
        f"Giá {pair['symbol_b']}: `${result['price_B']:.2f}`\n\n"
        f"🎯 *Gợi ý đóng lệnh*: khi spread về lại `{ex['exit_spread']:.4f}` "
        f"(z ≈ `{ex['exit_z']:.2f}`)\n"
        f"_Bot không tự động báo khi tới điểm đóng — bạn tự theo dõi bằng /check, "
        f"hoặc đặt take-profit/limit tương ứng ngay khi vào lệnh._"
    )


def build_check_message(pair: dict, result: dict) -> str:
    """Đoạn trạng thái của 1 cặp — cùng format với SIGNAL."""
    z = result["z"]
    ex = result.get("exit_level") or {}
    net = result.get("net_expected")
    net_txt = f"${net:.2f}" if net is not None else "n/a"
    exit_spread = ex.get("exit_spread", pair["mean"])
    exit_z = ex.get("exit_z", pair.get("exit_z", 0.0))
    return (
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{_direction_text(pair, z)}\n\n"
        f"Z-score: `{z:.2f}` (ngưỡng {pair['threshold']})\n"
        f"Spread hiện tại: `{result['spread']:.4f}`\n"
        f"*Net kỳ vọng: `{net_txt}`*\n\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | "
        f"Giá {pair['symbol_b']}: `${result['price_B']:.2f}`\n\n"
        f"🎯 *Gợi ý đóng lệnh*: khi spread về lại `{exit_spread:.4f}` "
        f"(z ≈ `{exit_z:.2f}`)"
    )


HELP_TEXT = (
    "*PAIRS BOT — MULTI-PAIR*\n"
    "Đang theo dõi 4 cặp:\n"
    "• `cl` — CL/BRENTOIL (WTI vs Brent) — Hyperliquid\n"
    "• `xyz100` — XYZ100/SP500 ⚠️ mẫu backtest còn nhỏ, chưa nên trade thật size lớn\n"
    "• `goldsilver` — GOLD/SILVER ⚠️ chỉ có lãi ròng ở ngưỡng z >= 2.5,"
    " mẫu backtest 29 trades/90 ngày — theo dõi sát trước khi tăng size\n"
    "• `xau` — XAU/XAUT (Variational) ⚠️ mean/std từ proxy 1H 60 ngày,"
    " mặc định threshold 1.0σ — đối chiếu funding với UI Omni\n\n"
    "Gõ /check để xem trạng thái TẤT CẢ cặp ngay lúc này.\n"
    "Gõ /check cl, /check xyz100, /check goldsilver hoặc /check xau "
    "để xem riêng 1 cặp.\n"
    "Tín hiệu tự động (khi đủ điều kiện vào lệnh) sẽ được bot gửi riêng mỗi "
    "5 phút cho từng cặp, không cần bạn phải hỏi."
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
        return True  # chưa cấu hình secret -> không chặn (không khuyến khích)
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") == TELEGRAM_WEBHOOK_SECRET


# =============================================================================
# ROUTE 1: /api — cron-job.org, quét định kỳ TẤT CẢ các cặp
# =============================================================================

@app.route("/api", methods=["GET", "POST"])
def scan_bot():
    if not check_cron_auth():
        return jsonify({"error": "Unauthorized"}), 401

    results = {}
    errors = {}
    for pair in PAIRS:
        try:
            result = evaluate_signal(pair, force_funding_check=False)
            results[pair["id"]] = result_to_json(result)
            if result["should_enter"]:
                send_telegram_message(build_signal_message(pair, result))
        except Exception as e:
            print(f"[ERROR] scan_bot pair={pair['id']}: {e}")
            errors[pair["id"]] = str(e)

    status_code = 200 if not errors or results else 500
    return jsonify({"results": results, "errors": errors}), status_code


# =============================================================================
# ROUTE 2: /api/webhook — Telegram tự gọi khi có tin nhắn mới
# =============================================================================

@app.route("/api/webhook", methods=["POST"])
def telegram_webhook():
    # Luôn trả 200 cho Telegram (kể cả sai secret/lỗi xử lý) để tránh
    # Telegram RETRY gửi lại cùng 1 Update nhiều lần.
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

    # Chỉ phản hồi đúng chat đã cấu hình -> chặn người lạ nhắn bot.
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return jsonify({"ok": True}), 200

    parts = text.split()
    command = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1].lower() if len(parts) > 1 else None

    try:
        if command in ("/start", "/help"):
            send_telegram_message(HELP_TEXT, chat_id=chat_id)
        elif command == "/check":
            if arg and arg in PAIRS_BY_ID:
                pair = PAIRS_BY_ID[arg]
                result = evaluate_signal(pair, force_funding_check=True)
                msg = f"*[CHECK] {pair['label']}*\n\n" + build_check_message(pair, result)
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
                    sections.append(build_check_message(pair, result))
                msg = "*[CHECK] PAIRS STATUS*\n\n" + "\n\n".join(sections)
                send_telegram_message(msg, chat_id=chat_id)
        elif command:
            send_telegram_message(
                "Lệnh không hợp lệ. Gõ /check để xem trạng thái tất cả cặp, "
                "hoặc /check <cl|xyz100|goldsilver|xau> cho 1 cặp cụ thể.",
                chat_id=chat_id,
            )
    except Exception as e:
        send_telegram_message(f"❌ Lỗi: {e}", chat_id=chat_id)

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)