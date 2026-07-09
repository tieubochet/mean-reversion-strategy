"""
=============================================================================
PAIRS TRADING SIGNAL BOT — xyz:CL (WTI) vs xyz:BRENTOIL (Brent) @ Hyperliquid
=============================================================================
File DUY NHẤT chứa toàn bộ config, hàm và logic. Deploy trên Vercel (Python
serverless function), scheduler dùng cron-job.org gọi endpoint mỗi 5 phút.

Endpoint: GET /api/index   (do file đặt tại api/index.py)

-----------------------------------------------------------------------------
LOGIC TỔNG QUAN mỗi lần chạy:
    1. Lấy giá hiện tại (nến m15 gần nhất) của 2 leg -> tính spread hiện tại.
    2. So spread hiện tại với SPREAD_MEAN / SPREAD_STD CỐ ĐỊNH (không tính lại
       rolling mỗi lần chạy) -> ra z-score.
    3. Nếu |z| < SIGNAL_THRESHOLD -> không làm gì, thoát.
    4. Nếu |z| >= SIGNAL_THRESHOLD -> ước tính:
         a. Lợi nhuận kỳ vọng nếu spread hồi về mean (dựa trên độ lệch hiện
            tại so với mean, quy đổi ra $ theo CAPITAL_PER_LEG).
         b. Funding rate hiện tại của cả 2 leg (Hyperliquid trả/thu mỗi giờ)
            -> quy đổi funding phải trả MỖI NGÀY cho vị thế dự kiến mở.
         c. Chi phí funding kỳ vọng = funding/ngày * số ngày hold kỳ vọng
            (lấy từ EXPECTED_HOLD_DAYS, ước lượng từ backtest Task 3).
         d. Trừ thêm phí trade cố định (FEE_PER_ROUND, từ 2.2bps x 4 fill).
       CHỈ GỬI TÍN HIỆU nếu: lợi nhuận kỳ vọng > phí trade + chi phí funding
       kỳ vọng. Nếu funding ăn hết lợi nhuận kỳ vọng -> bỏ qua, không vào lệnh.
-----------------------------------------------------------------------------

ENV VARS cần set trên Vercel (Project Settings -> Environment Variables):
    TELEGRAM_BOT_TOKEN     : token bot Telegram (từ @BotFather)
    TELEGRAM_CHAT_ID       : chat id nhận tín hiệu
    CRON_SECRET            : chuỗi bí mật tự chọn, cron-job.org gửi trong
                              header Authorization: Bearer <CRON_SECRET>
    SPREAD_MEAN             : mean cố định của spread ($/bbl) — recalibrate hàng tuần
    SPREAD_STD              : std cố định của spread ($/bbl) — recalibrate hàng tuần
    SIGNAL_THRESHOLD         : ngưỡng z-score để cân nhắc vào lệnh (mặc định 1.5)
    EXPECTED_HOLD_DAYS        : số ngày hold kỳ vọng dùng để ước tính funding
                                 cost (mặc định 0.264 ngày ~ 379.7 phút, lấy
                                 từ backtest threshold=1.5 trong Task 3)
    (Tất cả có default hợp lý trong code, không set env var vẫn chạy được)

RECALIBRATE HÀNG TUẦN (thủ công):
    1. Chạy lại script fetch_spread_stats.py trên dữ liệu 90 ngày mới nhất.
    2. Lấy Mean/Std mới từ spread_stats_table.md.
    3. Update SPREAD_MEAN / SPREAD_STD trên Vercel. Không cần redeploy.
    4. (Tuỳ chọn) chạy lại backtest_pairs.py để kiểm tra threshold 1.5 và
       EXPECTED_HOLD_DAYS còn hợp lý không, update nếu cần.
=============================================================================
"""

import os
import json
import time
import requests
from http.server import BaseHTTPRequestHandler

# =============================================================================
# CONFIG
# =============================================================================

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LEG_A_SYMBOL = "xyz:CL"          # WTI perp
LEG_B_SYMBOL = "xyz:BRENTOIL"    # Brent perp
INTERVAL = "15m"
HIP3_DEX = "xyz"                 # dex name cho các market builder-deployed "xyz:*"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# --- Thông số spread, cố định, recalibrate thủ công hàng tuần ---
SPREAD_MEAN = float(os.environ.get("SPREAD_MEAN", "-3.2858"))   # Task 2, 52 ngày data
SPREAD_STD = float(os.environ.get("SPREAD_STD", "0.4675"))       # Task 2, 52 ngày data
SIGNAL_THRESHOLD = float(os.environ.get("SIGNAL_THRESHOLD", "1.5"))  # Task 3 backtest

# --- Thông số vốn / phí, khớp giả định Task 3 ---
CAPITAL_PER_LEG = float(os.environ.get("CAPITAL_PER_LEG", "5000"))
FEE_BPS_PER_FILL = float(os.environ.get("FEE_BPS_PER_FILL", "2.2"))
FILLS_PER_ROUND = int(os.environ.get("FILLS_PER_ROUND", "4"))
FEE_PER_ROUND = (FEE_BPS_PER_FILL / 10_000) * CAPITAL_PER_LEG * FILLS_PER_ROUND

# --- Thời gian hold kỳ vọng, dùng để ước tính chi phí funding ---
# Mặc định lấy từ backtest threshold=1.5: hold trung bình 379.7 phút
EXPECTED_HOLD_DAYS = float(os.environ.get("EXPECTED_HOLD_DAYS", str(379.7 / 60 / 24)))


# =============================================================================
# HYPERLIQUID DATA FETCHING
# =============================================================================

def fetch_latest_close(coin: str) -> float:
    """Giá close của nến m15 gần nhất."""
    now_ms = int(time.time() * 1000)
    lookback_ms = 15 * 60 * 1000 * 3  # lấy dư 3 nến phòng nến cuối chưa đóng
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": INTERVAL,
            "startTime": now_ms - lookback_ms,
            "endTime": now_ms,
        },
    }
    resp = requests.post(HL_INFO_URL, json=payload, timeout=15)
    resp.raise_for_status()
    candles = resp.json()
    if not candles:
        raise RuntimeError(f"No candle data returned for {coin}")
    return float(candles[-1]["c"])


def fetch_funding_rates() -> dict:
    """Lấy funding rate HOURLY hiện tại của cả 2 leg qua metaAndAssetCtxs.
    Trả về dict {symbol: hourly_funding_rate (decimal)}.
    """
    payload = {"type": "metaAndAssetCtxs", "dex": HIP3_DEX}
    resp = requests.post(HL_INFO_URL, json=payload, timeout=15)
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()

    universe = meta["universe"]
    rates = {}
    for i, asset in enumerate(universe):
        name = asset["name"]
        if name in (LEG_A_SYMBOL, LEG_B_SYMBOL):
            rates[name] = float(asset_ctxs[i]["funding"])  # hourly rate, decimal

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


def estimate_expected_pnl(stats: dict) -> float:
    """Ước tính lợi nhuận gross ($) nếu spread hồi về SPREAD_MEAN, quy đổi
    theo CAPITAL_PER_LEG (cùng công thức barrel-equivalent dùng trong backtest)."""
    deviation = abs(stats["spread"] - SPREAD_MEAN)
    return deviation * (CAPITAL_PER_LEG / max(abs(stats["spread"]), 1))


def estimate_funding_cost(stats: dict, funding_rates: dict) -> dict:
    """Ước tính chi phí funding ròng MỖI NGÀY cho vị thế dự kiến, và tổng chi
    phí funding trong suốt EXPECTED_HOLD_DAYS.

    Quy ước: funding rate dương -> long trả, short nhận.
    z > 0  (spread quá cao) -> Short A, Long B
    z < 0  (spread quá thấp) -> Long A, Short B
    """
    daily_rate_a = funding_rates[LEG_A_SYMBOL] * 24
    daily_rate_b = funding_rates[LEG_B_SYMBOL] * 24

    if stats["z"] > 0:
        # Short A, Long B
        cost_a = -CAPITAL_PER_LEG * daily_rate_a   # short: nhận nếu rate dương -> cost âm
        cost_b = CAPITAL_PER_LEG * daily_rate_b    # long: trả nếu rate dương -> cost dương
    else:
        # Long A, Short B
        cost_a = CAPITAL_PER_LEG * daily_rate_a
        cost_b = -CAPITAL_PER_LEG * daily_rate_b

    daily_funding_cost = cost_a + cost_b  # có thể âm (nghĩa là được TRẢ, không phải mất)
    total_funding_cost = daily_funding_cost * EXPECTED_HOLD_DAYS

    return {
        "daily_rate_a": daily_rate_a,
        "daily_rate_b": daily_rate_b,
        "daily_funding_cost": daily_funding_cost,
        "total_funding_cost": total_funding_cost,
    }


def evaluate_signal() -> dict:
    stats = compute_zscore()
    z = stats["z"]

    result = {
        "z": z,
        "spread": stats["spread"],
        "price_A": stats["price_A"],
        "price_B": stats["price_B"],
        "should_enter": False,
        "reason": "z-score dưới ngưỡng",
    }

    if abs(z) < SIGNAL_THRESHOLD:
        return result

    funding_rates = fetch_funding_rates()
    funding = estimate_funding_cost(stats, funding_rates)
    expected_pnl = estimate_expected_pnl(stats)
    net_expected = expected_pnl - FEE_PER_ROUND - funding["total_funding_cost"]

    result.update({
        "expected_pnl": expected_pnl,
        "fee_per_round": FEE_PER_ROUND,
        "funding": funding,
        "net_expected": net_expected,
    })

    if net_expected > 0:
        result["should_enter"] = True
        result["reason"] = "Lợi nhuận kỳ vọng > phí + funding cost"
    else:
        result["reason"] = "Funding cost + phí ăn hết lợi nhuận kỳ vọng -> bỏ qua"

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
    msg = (
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
        f"Giá xyz:CL: `${result['price_A']:.2f}` | Giá xyz:BRENTOIL: `${result['price_B']:.2f}`"
    )
    return msg


# =============================================================================
# HTTP HANDLER (Vercel entrypoint)
# =============================================================================

class handler(BaseHTTPRequestHandler):
    def _handle(self):
        """Logic dùng chung: cron-job.org gọi bằng POST (mỗi 5 phút quét tín
        hiệu), GET vẫn để lại cho tiện test thủ công bằng curl/trình duyệt."""
        if CRON_SECRET:
            auth_header = self.headers.get("Authorization", "")
            if auth_header != f"Bearer {CRON_SECRET}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Unauthorized")
                return

        try:
            result = evaluate_signal()

            if result["should_enter"]:
                send_telegram_message(build_signal_message(result))

            response = {
                "z": round(result["z"], 3),
                "spread": round(result["spread"], 4),
                "should_enter": result["should_enter"],
                "reason": result["reason"],
            }
            if "net_expected" in result:
                response["net_expected"] = round(result["net_expected"], 2)

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
        # cron-job.org ping bằng POST mỗi 5 phút -> đường chính
        self._handle()

    def do_GET(self):
        # giữ lại để test thủ công bằng curl/trình duyệt
        self._handle()