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
                               (pair_id: "cl" hoặc "xyz100")

CẶP ĐANG THEO DÕI:
    1. cl        — xyz:CL (WTI) vs xyz:BRENTOIL   — spread = price_A - price_B ($/bbl)
    2. xyz100    — xyz:XYZ100 vs xyz:SP500        — spread = ln(price_A / price_B)
                   (log-ratio, vì 2 chỉ số có scale giá khác xa nhau)

QUAN TRỌNG VỀ VERCEL ROUTING: xem vercel.json — bắt buộc có "rewrites" trỏ
"/api" và "/api/webhook" về "/api/index", nếu không sẽ bị 404 ở tầng Vercel.

ENV VARS (Project Settings -> Environment Variables trên Vercel):
    Chung: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET,
           TELEGRAM_WEBHOOK_SECRET, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    Theo từng cặp (suffix _CL hoặc _XYZ100), tất cả có default hợp lý:
           SPREAD_MEAN_<X>, SPREAD_STD_<X>, SIGNAL_THRESHOLD_<X>,
           EXIT_Z_THRESHOLD_<X>, EXPECTED_HOLD_DAYS_<X>, CAPITAL_PER_LEG_<X>

⚠️ CẶP xyz100/SP500 MỚI, MẪU BACKTEST NHỎ (9 trades, ~53 ngày data, CHƯA
   cộng funding rate lịch sử vào PnL backtest) — xem README mục cảnh báo.
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


def compute_spread(price_a: float, price_b: float, spread_type: str) -> float:
    if spread_type == "logratio":
        return math.log(price_a / price_b)
    return price_a - price_b


def compute_zscore(pair: dict, with_funding: bool) -> dict:
    """Lấy giá 2 leg (và funding, nếu cần) SONG SONG qua thread pool -> giảm
    thời gian chờ tối đa, tránh timeout function trên Vercel."""
    symbol_a, symbol_b = pair["symbol_a"], pair["symbol_b"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_a = ex.submit(fetch_latest_close, symbol_a)
        fut_b = ex.submit(fetch_latest_close, symbol_b)
        fut_funding = ex.submit(fetch_funding_rates, symbol_a, symbol_b) if with_funding else None

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

    funding_rates = stats["funding_rates"] or fetch_funding_rates(pair["symbol_a"], pair["symbol_b"])
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
    f = result["funding"]
    ex = result["exit_level"]
    return (
        f"*PAIRS SIGNAL — {pair['label']}*\n"
        f"{_direction_text(pair, z)}\n\n"
        f"Z-score: `{z:.2f}` (ngưỡng {pair['threshold']})\n"
        f"Spread hiện tại: `{result['spread']:.4f}`\n"
        f"Mean cố định: `{pair['mean']:.4f}` | Std cố định: `{pair['std']:.4f}`\n\n"
        f"Lợi nhuận kỳ vọng (nếu hồi mean): `${result['expected_pnl']:.2f}`\n"
        f"Phí trade ({FILLS_PER_ROUND} fill x {FEE_BPS_PER_FILL}bps): `${result['fee_per_round']:.2f}`\n"
        f"Funding/ngày dự kiến: `${f['daily_funding_cost']:.2f}`\n"
        f"Funding cost ước tính ({pair['expected_hold_days']:.2f} ngày hold): `${f['total_funding_cost']:.2f}`\n"
        f"*Net kỳ vọng: `${result['net_expected']:.2f}`*\n\n"
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | Giá {pair['symbol_b']}: `${result['price_B']:.2f}`\n\n"
        f"🎯 *Gợi ý đóng lệnh*: khi spread về lại `{ex['exit_spread']:.4f}` "
        f"(z ≈ `{ex['exit_z']:.2f}`)\n"
        f"_Bot không tự động báo khi tới điểm đóng — bạn tự theo dõi bằng /check, "
        f"hoặc đặt take-profit/limit tương ứng ngay khi vào lệnh._"
    )


def build_check_message(pair: dict, result: dict) -> str:
    """Đoạn trạng thái của 1 cặp — dùng ghép trong /check."""
    z = result["z"]
    status_icon = "✅" if result["should_enter"] else "⏸"

    lines = [
        f"*{pair['label']}*",
        f"{status_icon} {result['reason']}",
        f"Z-score: `{z:.2f}` (ngưỡng {pair['threshold']})",
        f"Spread hiện tại: `{result['spread']:.4f}`",
        f"Mean cố định: `{pair['mean']:.4f}` | Std cố định: `{pair['std']:.4f}`",
        f"Giá {pair['symbol_a']}: `${result['price_A']:.2f}` | Giá {pair['symbol_b']}: `${result['price_B']:.2f}`",
    ]

    if "net_expected" in result:
        f = result["funding"]
        ex = result["exit_level"]
        lines += [
            f"Lợi nhuận kỳ vọng: `${result['expected_pnl']:.2f}`",
            f"Phí trade: `${result['fee_per_round']:.2f}`",
            f"Funding/ngày dự kiến: `${f['daily_funding_cost']:.2f}`",
            f"Funding cost ước tính ({pair['expected_hold_days']:.2f} ngày): `${f['total_funding_cost']:.2f}`",
            f"*Net kỳ vọng: `${result['net_expected']:.2f}`*",
            f"🎯 Gợi ý đóng lệnh (nếu đang mở): spread về `{ex['exit_spread']:.4f}` (z ≈ `{ex['exit_z']:.2f}`)",
        ]

    if result["should_enter"]:
        lines.append(f"→ {_direction_text(pair, z)}")

    return "\n".join(lines)


HELP_TEXT = (
    "*PAIRS BOT — MULTI-PAIR*\n"
    "Đang theo dõi 2 cặp:\n"
    "• `cl` — CL/BRENTOIL (WTI vs Brent)\n"
    "• `xyz100` — XYZ100/SP500 ⚠️ mẫu backtest còn nhỏ, chưa nên trade thật size lớn\n\n"
    "Gõ /check để xem trạng thái TẤT CẢ cặp ngay lúc này.\n"
    "Gõ /check cl hoặc /check xyz100 để xem riêng 1 cặp.\n"
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
                "hoặc /check <cl|xyz100> cho 1 cặp cụ thể.",
                chat_id=chat_id,
            )
    except Exception as e:
        send_telegram_message(f"❌ Lỗi: {e}", chat_id=chat_id)

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)