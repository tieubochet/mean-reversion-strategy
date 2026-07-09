"""
=============================================================================
PAIRS TRADING SIGNAL BOT — xyz:CL (WTI) vs xyz:BRENTOIL (Brent) @ Hyperliquid
=============================================================================
Module lõi (KHÔNG phải endpoint) — chứa toàn bộ config, hàm fetch data và
logic tính tín hiệu. Được import bởi:
    - api/index.py  -> endpoint POST /api/index, cron-job.org ping mỗi 5 phút.
                        GỬI TELEGRAM khi có tín hiệu đạt ngưỡng vào lệnh
                        (should_enter=True).
    - api/check.py  -> endpoint GET/POST /api/check, quét thủ công bất cứ
                        lúc nào. LUÔN GỬI TELEGRAM 1 tin báo trạng thái hiện
                        tại (dù có tín hiệu vào lệnh hay không).

Tóm tắt 2 trường hợp bot gửi tin Telegram:
    1. /api/index phát hiện |z| >= SIGNAL_THRESHOLD VÀ net kỳ vọng > 0
       -> gửi tin "PAIRS SIGNAL" (build_signal_message).
    2. Người dùng tự gọi /api/check -> LUÔN gửi 1 tin "CHECK PAIRS STATUS"
       báo trạng thái hiện tại (build_check_message), bất kể z có vượt
       ngưỡng hay không.

Gom mọi thứ dùng chung ở đây để 2 endpoint không bị trùng lặp code.

-----------------------------------------------------------------------------
LOGIC TỔNG QUAN evaluate_signal():
    1. Lấy giá hiện tại (nến m15 gần nhất) của 2 leg -> tính spread hiện tại.
    2. So spread hiện tại với SPREAD_MEAN / SPREAD_STD CỐ ĐỊNH (không tính lại
       rolling mỗi lần chạy) -> ra z-score.
    3. Nếu |z| < SIGNAL_THRESHOLD -> should_enter = False, dừng ở đây.
    4. Nếu |z| >= SIGNAL_THRESHOLD -> ước tính:
         a. Lợi nhuận kỳ vọng nếu spread hồi về mean (quy đổi ra $ theo
            CAPITAL_PER_LEG).
         b. Funding rate hiện tại của cả 2 leg (Hyperliquid trả/thu mỗi giờ)
            -> quy đổi funding phải trả MỖI NGÀY cho vị thế dự kiến mở.
         c. Chi phí funding kỳ vọng = funding/ngày * EXPECTED_HOLD_DAYS.
         d. Trừ thêm phí trade cố định (FEE_PER_ROUND, từ 2.2bps x 4 fill).
       should_enter = True chỉ khi: lợi nhuận kỳ vọng > phí trade + funding
       cost kỳ vọng.
-----------------------------------------------------------------------------

ENV VARS cần set trên Vercel (Project Settings -> Environment Variables):
    TELEGRAM_BOT_TOKEN     : token bot Telegram (từ @BotFather)
    TELEGRAM_CHAT_ID       : chat id nhận tín hiệu
    CRON_SECRET            : chuỗi bí mật tự chọn, xác thực request gọi vào
                              /api/index và /api/check qua header
                              Authorization: Bearer <CRON_SECRET>
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
import time
import requests

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

# Ngưỡng z-score gợi ý ĐÓNG lệnh — mặc định 0.0, khớp đúng quy tắc exit dùng
# trong backtest_pairs.py Task 3 (thoát khi spread hồi về đúng Mean, z=0).
# Có thể nới ra (VD 0.3) nếu muốn thoát sớm hơn, chấp nhận ăn ít hơn để giảm
# rủi ro spread không hồi hẳn về mean.
EXIT_Z_THRESHOLD = float(os.environ.get("EXIT_Z_THRESHOLD", "0.0"))

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
    resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
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
    resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
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


def suggest_exit_level(z: float) -> dict:
    """Gợi ý mức spread nên đóng lệnh, dựa trên EXIT_Z_THRESHOLD.

    z > 0 (đang Short spread) -> đóng khi z giảm về EXIT_Z_THRESHOLD (spread giảm xuống)
    z < 0 (đang Long spread)  -> đóng khi z tăng lên -EXIT_Z_THRESHOLD (spread tăng lên)
    """
    if z > 0:
        exit_z = EXIT_Z_THRESHOLD
    else:
        exit_z = -EXIT_Z_THRESHOLD
    exit_z = exit_z if exit_z != 0 else 0.0  # tránh hiển thị -0.00

    exit_spread = SPREAD_MEAN + exit_z * SPREAD_STD
    return {"exit_z": exit_z, "exit_spread": exit_spread}


def estimate_expected_pnl(stats: dict) -> float:
    """Ước tính lợi nhuận gross ($) nếu spread hồi về SPREAD_MEAN.

    Số barrel/leg = CAPITAL_PER_LEG / giá trung bình 2 leg (KHÔNG chia cho
    giá spread — đó là lỗi cũ khiến kết quả bị thổi phồng ~17 lần, vì giá
    spread (~$4-5) nhỏ hơn nhiều so với giá tài sản thực (~$74-79)).
    """
    avg_price = (stats["price_A"] + stats["price_B"]) / 2
    barrels_per_leg = CAPITAL_PER_LEG / max(avg_price, 1)
    deviation = abs(stats["spread"] - SPREAD_MEAN)
    return deviation * barrels_per_leg


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


def evaluate_signal(force_funding_check: bool = False) -> dict:
    """Quét tín hiệu hiện tại.

    force_funding_check: nếu True, vẫn lấy funding + tính net kỳ vọng ngay cả
    khi |z| chưa vượt ngưỡng (hữu ích cho /check để xem trước funding cost
    hiện tại là bao nhiêu, dù chưa có tín hiệu vào lệnh).
    """
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

    if abs(z) < SIGNAL_THRESHOLD and not force_funding_check:
        return result

    funding_rates = fetch_funding_rates()
    funding = estimate_funding_cost(stats, funding_rates)
    expected_pnl = estimate_expected_pnl(stats)
    net_expected = expected_pnl - FEE_PER_ROUND - funding["total_funding_cost"]
    exit_level = suggest_exit_level(z)

    result.update({
        "expected_pnl": expected_pnl,
        "fee_per_round": FEE_PER_ROUND,
        "funding": funding,
        "net_expected": net_expected,
        "exit_level": exit_level,
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


def build_check_message(result: dict) -> str:
    """Tin nhắn Telegram gửi mỗi lần gọi /check — báo trạng thái hiện tại,
    kể cả khi z chưa vượt ngưỡng (khác build_signal_message vốn chỉ dùng khi
    should_enter=True)."""
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


def build_signal_message(result: dict) -> str:
    z = result["z"]
    direction = "🔴 SHORT SPREAD (Short xyz:CL / Long xyz:BRENTOIL)" if z > 0 \
        else "🟢 LONG SPREAD (Long xyz:CL / Short xyz:BRENTOIL)"

    f = result["funding"]
    ex = result["exit_level"]
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
        f"Giá xyz:CL: `${result['price_A']:.2f}` | Giá xyz:BRENTOIL: `${result['price_B']:.2f}`\n\n"
        f"🎯 *Gợi ý đóng lệnh*: khi spread về lại `${ex['exit_spread']:.3f}/bbl` "
        f"(z ≈ `{ex['exit_z']:.2f}`)\n"
        f"_Bot không tự động báo khi tới điểm đóng — bạn tự theo dõi bằng /check, "
        f"hoặc đặt take-profit/limit tương ứng ngay khi vào lệnh._"
    )
    return msg


def result_to_json(result: dict) -> dict:
    """Rút gọn result dict thành response JSON gọn cho endpoint trả về."""
    response = {
        "z": round(result["z"], 3),
        "spread": round(result["spread"], 4),
        "should_enter": result["should_enter"],
        "reason": result["reason"],
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
    """True nếu request hợp lệ (hoặc không set CRON_SECRET nên bỏ qua check)."""
    if not CRON_SECRET:
        return True
    return headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"
