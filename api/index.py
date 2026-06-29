import os
import requests
from flask import Flask, request

app = Flask(__name__)

# ==================== CONFIG ====================
# Điền chính xác Tên hiển thị (Ticker) bạn thấy trên giao diện sàn.
# Ví dụ trên sàn ghi là WTIOIL-USDC thì bạn chỉ điền "WTIOIL".
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "WTIOIL",         # ← Ticker hiển thị chuẩn của WTI trên sàn
        "name_b": "Brent (B)",
        "symbol_b": "BRENTOIL",      # ← Ticker hiển thị chuẩn của Brent trên sàn
        "mean": -3.69,
        "std": 2.52,
        "use_zscore": True,
        "long_z_threshold": -1.0,
        "short_z_threshold": 0.8,
        "exit_z_threshold": 0.2,
        "vol_per_leg": 700,
        "avg_hold_hours": 69,
        "fee_bps": 0.00022,
        "min_net_pnl": 30,
        "max_funding_loss": 40,
    }
}

# ==================== HÀM HỖ TRỢ ====================
def calculate_z_score(spread, mean, std):
    if std == 0:
        return 0
    return (spread - mean) / std

def get_hyperliquid_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}

    prices = {}
    funding_dict = {}
    ticker_to_id_map = {} # Dùng để map từ "WTIOIL" -> "@107"

    # 1. Lấy dữ liệu meta để xây dựng Bản đồ Ticker Mapping chuẩn xác
    try:
        meta_resp = requests.post(
            url, headers=headers, json={"type": "metaAndAssetCtxs"}, timeout=10
        ).json()

        if isinstance(meta_resp, list) and len(meta_resp) >= 2:
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]

            for i, asset in enumerate(universe):
                # Tên nội bộ (Dạng "@107" hoặc "BTC" tùy loại tài sản)
                internal_name = asset.get("name", "")
                
                # Tên hiển thị (Dạng "WTIOIL-USDC" hoặc "BTC-PERP")
                # Một số API cũ hoặc môi trường HIP-3 trả về tên thô hoặc cấu trúc mở rộng, 
                # ta chuẩn hóa loại bỏ hậu tố "-USDC" hoặc "-PERP" để lấy Ticker gốc
                display_name = internal_name.split("-")[0].replace("@", "").upper()
                
                if internal_name:
                    # Map cả 2 dạng để đảm bảo tìm kiểu gì cũng trúng
                    ticker_to_id_map[internal_name.upper()] = internal_name
                    # Map từ "WTIOIL" -> "@107"
                    clean_ticker = internal_name.replace("@", "").split("-")[0].upper()
                    ticker_to_id_map[clean_ticker] = internal_name

                    # Lưu dữ liệu funding rate tương ứng cho mã internal_name
                    if i < len(asset_ctxs):
                        funding_dict[internal_name] = float(asset_ctxs[i].get("funding", 0))

    except Exception as e:
        print(f"Lỗi phân tích metaAndAssetCtxs: {e}")

    # 2. Lấy dữ liệu giá thực tế từ allMids
    try:
        mids_resp = requests.post(
            url, headers=headers, json={"type": "allMids"}, timeout=10
        ).json()

        if isinstance(mids_resp, dict):
            prices = {k: float(v) for k, v in mids_resp.items() if v}
    except Exception as e:
        print(f"Lỗi lấy dữ liệu allMids: {e}")

    return prices, funding_dict, ticker_to_id_map

def calc_net_funding(funding_a, funding_b, is_long_a, vol):
    net_rate = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    return net_rate * 24 * vol, net_rate * 24 * 365 * 100

def evaluate_signal(config, current_spread, is_long_a, net_usd_day, price_a):
    vol = config["vol_per_leg"]
    mean = config["mean"]
    std = config["std"]
    avg_hold_days = config["avg_hold_hours"] / 24
    fee_bps = config["fee_bps"]

    units = vol / price_a if price_a > 0 else 0
    z_score = calculate_z_score(current_spread, mean, std)

    if is_long_a:
        spread_to_mean = mean - current_spread
    else:
        spread_to_mean = current_spread - mean

    gross = spread_to_mean * units
    funding_total = net_usd_day * avg_hold_days
    fee = vol * 2 * fee_bps * 2
    net_pnl = gross + funding_total - fee

    min_pnl = config.get("min_net_pnl", 0)
    max_loss = config.get("max_funding_loss", 50)

    if net_pnl >= min_pnl:
        quality, should_send, rec = "good", True, "✅ Signal tốt - Nên vào lệnh"
    elif funding_total >= -max_loss and net_pnl > -30:
        quality, should_send, rec = "acceptable", True, "🟡 Signal chấp nhận được"
    else:
        quality, should_send, rec = "poor", False, "🚫 Signal yếu - Bỏ qua"

    breakeven = abs(funding_total) / units if funding_total < 0 and units > 0 else 0
    should_exit = abs(z_score) <= config.get("exit_z_threshold", 0.2)

    return {
        "units": round(units, 4),
        "z_score": round(z_score, 2),
        "gross_spread_pnl": round(gross, 2),
        "funding_net_total": round(funding_total, 2),
        "fee_total": round(fee, 2),
        "net_pnl": round(net_pnl, 2),
        "avg_hold_days": round(avg_hold_days, 1),
        "should_send": should_send,
        "quality": quality,
        "recommendation": rec,
        "breakeven_spread_needed": round(breakeven, 2),
        "is_funding_negative": funding_total < 0,
        "should_exit": should_exit,
    }

def build_check_message(prices, funding_rates, ticker_map):
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường - Hyperliquid*\n🕐 `{now}`\n"]

    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a_cfg = cfg["symbol_a"].upper()
        sym_b_cfg = cfg["symbol_b"].upper()
        name_a = cfg["name_a"]
        name_b = cfg["name_b"]

        # Tra cứu mã ID thực tế (ví dụ: tìm "WTIOIL" -> trả về "@107")
        sym_a = ticker_map.get(sym_a_cfg, cfg["symbol_a"])
        sym_b = ticker_map.get(sym_b_cfg, cfg["symbol_b"])

        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        # Khử lỗi trường hợp sàn đổi format đặt tên
        if price_a == 0 or price_b == 0:
            # Quét quét chuỗi thông minh (Fallback)
            for k in prices.keys():
                if sym_a_cfg in k.upper(): sym_a = k
                if sym_b_cfg in k.upper(): sym_b = k
            price_a = float(prices.get(sym_a, 0))
            price_b = float(prices.get(sym_b, 0))

        if price_a == 0 or price_b == 0:
            lines.append(f"❌ *{name_a} vs {name_b}*: Không lấy được giá\n(Mã tìm kiếm: Ticker `{sym_a_cfg}` -> ID trên sàn `{sym_a}`)\n")
            continue

        spread = price_a - price_b
        mean = cfg["mean"]
        std = cfg["std"]
        z_score = calculate_z_score(spread, mean, std)

        use_zscore = cfg.get("use_zscore", False)
        long_z = cfg.get("long_z_threshold", -1.0)
        short_z = cfg.get("short_z_threshold", 0.8)
        long_t = cfg.get("long_threshold", -3.7)
        short_t = cfg.get("short_threshold", -2.8)
        vol = cfg["vol_per_leg"]

        if use_zscore:
            if z_score <= long_z:
                status = "🟢 ĐẠT NGƯỠNG LONG"
            elif z_score >= short_z:
                status = "🔴 ĐẠT NGƯỠNG SHORT"
            else:
                status = "⏳ Trong vùng trung tính"
            dist_to_long = f"{z_score - long_z:+.2f}"
            dist_to_short = f"{short_z - z_score:+.2f}"
        else:
            if spread <= long_t:
                status = "🟢 ĐẠT NGƯỠNG LONG"
            elif spread >= short_t:
                status = "🔴 ĐẠT NGƯỠNG SHORT"
            else:
                status = "⏳ Trong vùng trung tính"
            dist_to_long = f"{spread - long_t:+.2f}"
            dist_to_short = f"{short_t - spread:+.2f}"

        # Lấy funding rate theo đúng mã ID hệ thống
        fa = funding_rates.get(sym_a, 0)
        fb = funding_rates.get(sym_b, 0)
        f_long, apr_long = calc_net_funding(fa, fb, True, vol)
        f_short, apr_short = calc_net_funding(fa, fb, False, vol)

        def fmt_f(usd, apr):
            icon = "✅" if usd >= 0 else "🔴"
            sign = "+" if usd >= 0 else ""
            return f"{icon} `{sign}${usd:.2f}/ngày` (APR `{apr:+.1f}%`)"

        block = (
            f"─────────────────────\n"
            f"🛢 *{name_a} vs {name_b}*\n"
            f"  Giá {sym_a_cfg} (`{sym_a}`): `${price_a:.4f}`\n"
            f"  Giá {sym_b_cfg} (`{sym_b}`): `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.2f}`\n"
            f"  **Z-Score: `{z_score:+.2f}`**\n"
            f"  Mean: `{mean}` | Std: `{std}`\n\n"
            f"  📍 *Trạng thái:* {status}\n"
            f"  ↳ Cách ngưỡng Long : `{dist_to_long}`\n"
            f"  ↳ Cách ngưỡng Short: `{dist_to_short}`\n\n"
            f"  💸 *Funding* (vốn `${vol:,}/leg`):\n"
            f"  Long {sym_a_cfg}+Short {sym_b_cfg}: {fmt_f(f_long, apr_long)}\n"
            f"  Short {sym_a_cfg}+Long {sym_b_cfg}: {fmt_f(f_short, apr_short)}\n"
        )
        lines.append(block)

    lines.append("─────────────────────\n💡 Dùng `/check` để cập nhật lại")
    return "\n".join(lines)

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

# ==================== API ====================
@app.route("/api", methods=["POST"])
def scan_bot():
    pass

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    update = request.get_json(silent=True)
    if not update:
        return "OK", 200

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return "OK", 200

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip().lower()

    if text.startswith("/check"):
        try:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang kết nối API sàn và đồng bộ mã ID...")
            prices, funding, ticker_map = get_hyperliquid_data()
            reply = build_check_message(prices, funding, ticker_map)
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, reply)
        except Exception as e:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: {str(e)}")

    elif text.startswith("/help"):
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "🤖 Bot Spread Trading\n/check - Snapshot + Z-Score")

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)