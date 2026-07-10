"""
=============================================================================
Endpoint: POST/GET /api/index — PAIRS TRADING SIGNAL BOT (CL vs BRENTOIL)
=============================================================================
1 FILE DUY NHẤT, 1 URL DUY NHẤT, phục vụ CẢ 2 nguồn gọi tới, tự phân biệt
bằng header của request:

  A) cron-job.org ping mỗi 5 phút (POST, header "Authorization: Bearer
     <CRON_SECRET>") -> quét tín hiệu, CHỈ gửi Telegram khi đủ điều kiện vào
     lệnh (should_enter=True).

  B) Telegram tự POST tới đây mỗi khi có tin nhắn mới trong chat (đã đăng ký
     qua setWebhook, header "X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_
     WEBHOOK_SECRET>") -> nếu tin nhắn là lệnh "/check", quét tín hiệu và
     LUÔN trả lời ngay vào đúng chat đó (dù có tín hiệu vào lệnh hay không).

Cách phân biệt: dựa vào có header "X-Telegram-Bot-Api-Secret-Token" hay
không — chỉ Telegram gửi header này (do mình khai báo secret_token lúc gọi
setWebhook), cron-job.org không biết gì về header đó.

TẠI SAO GỘP 1 FILE: chỉ có 1 URL để cấu hình cả 2 nơi (cron-job.org VÀ
Telegram setWebhook đều trỏ về "https://your-project.vercel.app/api/index"),
và logic quét tín hiệu (evaluate_signal) chỉ viết 1 lần duy nhất, không phải
lặp lại ở nhiều file như trước.

ENV VARS (Project Settings -> Environment Variables trên Vercel):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET, TELEGRAM_WEBHOOK_SECRET,
    SPREAD_MEAN, SPREAD_STD, SIGNAL_THRESHOLD, EXIT_Z_THRESHOLD,
    EXPECTED_HOLD_DAYS, CAPITAL_PER_LEG, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    (tất cả có default hợp lý trong code, không set vẫn chạy được — riêng
    CRON_SECRET và TELEGRAM_WEBHOOK_SECRET nên set để tránh bị gọi giả mạo)
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
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

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


def compute_zscore(with_funding: bool) -> dict:
    """
    Lấy giá 2 leg (và funding rate, nếu with_funding=True) SONG SONG qua
    thread pool thay vì tuần tự -> giảm thời gian chờ tối đa từ việc cộng
    dồn từng lệnh xuống còn ~ thời gian của lệnh chậm nhất. Quan trọng với
    nhánh Telegram /check (luôn cần funding) để tránh timeout function trên
    Vercel khi phải chờ nhiều API Hyperliquid liên tiếp.
    """
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_a = ex.submit(fetch_latest_close, LEG_A_SYMBOL)
        fut_b = ex.submit(fetch_latest_close, LEG_B_SYMBOL)
        fut_funding = ex.submit(fetch_funding_rates) if with_funding else None

        price_a = fut_a.result()
        price_b = fut_b.result()
        funding_rates = fut_funding.result() if fut_funding else None

    spread = price_a - price_b
    z = (spread - SPREAD_MEAN) / SPREAD_STD if SPREAD_STD > 0 else 0.0
    return {
        "spread": spread, "z": z, "price_A": price_a, "price_B": price_b,
        "funding_rates": funding_rates,
    }


# =============================================================================
# SIGNAL LOGIC
# =============================================================================

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
    """
    force_funding_check=False (nhánh cron /index): nếu |z| chưa tới ngưỡng,
    dừng ngay sau khi lấy giá — không tốn thêm API call funding.
    force_funding_check=True (nhánh Telegram /check): luôn lấy funding ngay
    từ đầu (chạy song song cùng giá) để trả lời đầy đủ mỗi lần được hỏi.
    """
    stats = compute_zscore(with_funding=force_funding_check)
    z = stats["z"]

    result = {
        "z": z, "spread": stats["spread"],
        "price_A": stats["price_A"], "price_B": stats["price_B"],
        "should_enter": False, "reason": "z-score dưới ngưỡng",
    }

    if abs(z) < SIGNAL_THRESHOLD and not force_funding_check:
        return result

    funding_rates = stats["funding_rates"] or fetch_funding_rates()
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

def send_telegram_message(text: str, chat_id: str = None):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")
    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not target_chat_id:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID env var")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()


def build_signal_message(result: dict) -> str:
    """Tin nhắn chủ động khi cron phát hiện đủ điều kiện vào lệnh."""
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


def build_check_message(result: dict) -> str:
    """Tin nhắn trả lời khi user chủ động gõ /check trong Telegram."""
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


HELP_TEXT = (
    "*PAIRS BOT — CL/BRENTOIL*\n"
    "Gõ /check để xem trạng thái z-score, funding cost và gợi ý vào/đóng "
    "lệnh ngay lúc này.\n"
    "Tín hiệu tự động (khi đủ điều kiện vào lệnh) sẽ được bot gửi riêng mỗi "
    "5 phút, không cần bạn phải hỏi."
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


# =============================================================================
# REQUEST ROUTING — phân biệt cron-job.org vs Telegram webhook
# =============================================================================

def is_telegram_update(parsed_body) -> bool:
    """
    Nhận diện Telegram bằng CẤU TRÚC JSON body (luôn có field "update_id")
    thay vì dựa vào header "X-Telegram-Bot-Api-Secret-Token". Lý do đổi: nếu
    header vì bất kỳ lý do gì (setWebhook thiếu tham số, header bị chặn/đổi
    tên khi đi qua hạ tầng trung gian...) không tới được code, nhận diện qua
    body vẫn hoạt động đúng — không có single point of failure ở 1 header.
    """
    return isinstance(parsed_body, dict) and "update_id" in parsed_body


def check_telegram_secret(headers) -> bool:
    if not TELEGRAM_WEBHOOK_SECRET:
        return True  # chưa cấu hình secret -> không chặn (không khuyến khích)
    return headers.get("X-Telegram-Bot-Api-Secret-Token", "") == TELEGRAM_WEBHOOK_SECRET


def check_cron_auth(headers) -> bool:
    if not CRON_SECRET:
        return True
    return headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"


def handle_telegram_update(update: dict):
    """Xử lý 1 Update Telegram (tin nhắn mới) -> trả lời nếu là lệnh /check."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return  # bỏ qua các loại update khác (channel_post, callback_query...)

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    # Chỉ phản hồi đúng chat đã cấu hình -> chặn người lạ nhắn bot kích hoạt
    # quét tín hiệu tốn API quota.
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return

    command = text.split()[0].split("@")[0].lower() if text else ""

    if command in ("/start", "/help"):
        send_telegram_message(HELP_TEXT, chat_id=chat_id)
    elif command == "/check":
        result = evaluate_signal(force_funding_check=True)
        send_telegram_message(build_check_message(result), chat_id=chat_id)
    elif command:
        send_telegram_message(
            "Lệnh không hợp lệ. Gõ /check để xem trạng thái pairs hiện tại.",
            chat_id=chat_id,
        )
    # text rỗng hoặc không phải lệnh -> im lặng, không phản hồi


def handle_cron_scan() -> dict:
    """Quét tín hiệu định kỳ (cron-job.org) -> chỉ gửi Telegram nếu đủ điều kiện."""
    result = evaluate_signal(force_funding_check=False)
    if result["should_enter"]:
        send_telegram_message(build_signal_message(result))
    return result_to_json(result)


# =============================================================================
# HTTP HANDLER (Vercel entrypoint)
# =============================================================================

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        try:
            parsed_body = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed_body = {}

        if is_telegram_update(parsed_body):
            # --- Nhánh Telegram webhook ---
            # Luôn trả 200 cho Telegram (kể cả lỗi xử lý bên trong, kể cả
            # sai secret) để tránh Telegram RETRY gửi lại cùng 1 Update
            # nhiều lần — pending_update_count sẽ dồn lại nếu không làm vậy.
            if check_telegram_secret(self.headers):
                try:
                    handle_telegram_update(parsed_body)
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return

        # --- Nhánh cron-job.org ---
        if not check_cron_auth(self.headers):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            response = handle_cron_scan()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        # Test tay nhanh qua browser/curl -X GET — chạy giống hệt nhánh cron
        # (không cần giả header Telegram). Vẫn yêu cầu CRON_SECRET nếu có set.
        if not check_cron_auth(self.headers):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            response = handle_cron_scan()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())