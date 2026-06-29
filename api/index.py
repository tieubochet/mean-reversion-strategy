import os
import requests
from flask import Flask, request

app = Flask(__name__)

# ==================== CONFIG ====================
# Điền THẲNG mã hệ thống dạng @ID đã được xác thực từ log để bot chạy chính xác 100%
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTIOIL-USDC",
        "symbol_a": "@107",             # Mã cứng của WTI trên Hyperliquid
        "name_b": "BRENTOIL",
        "symbol_b": "@156",             # Mã cứng của Brent trên Hyperliquid
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

    # === 1. LẤY GIÁ CÁC CẶP PERP (BAO GỒM CẢ HIP-3) ===
    try:
        # Sử dụng metaAndAssetCtxs để lấy đồng thời trạng thái giá mid của toàn bộ vũ trụ (universe)
        meta_resp = requests.post(
            url, headers=headers, json={"type": "metaAndAssetCtxs"}  # TODO: iterate all dexes for HIP-3, timeout=10
        ).json()

        if isinstance(meta_resp, list) and len(meta_resp) >= 2:
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]

            # Quét qua danh sách universe để bóc tách thông tin tên hiển thị động
            for i, asset in enumerate(universe):
                # Tên hệ thống (Có thể là '@107' hoặc 'WTIOIL-USDC' tùy thuộc vào phiên bản cập nhật HIP-3)
                internal_name = asset.get("name", "")
                
                # Trích xuất dữ liệu context tài sản tại index tương ứng
                if i < len(asset_ctxs) and internal_name:
                    ctx = asset_ctxs[i]
                    
                    # Lấy giá mid-price chính xác đang active từ context tài sản
                    mid_price = float(ctx.get("midPx") or ctx.get("midPrice") or 0)
                    funding_rate = float(ctx.get("funding", 0))
                    
                    if mid_price > 0:
                        prices[internal_name.upper()] = mid_price
                        # Thử map thêm trường hợp loại bỏ các ký tự đặc biệt để bot đối chiếu chuỗi dễ hơn
                        clean_name = internal_name.replace("@", "").upper()
                        prices[clean_name] = mid_price
                        
                    funding_dict[internal_name.upper()] = funding_rate
                    funding_dict[clean_name] = funding_rate

    except Exception as e:
        print(f"Lỗi khi xử lý cấu trúc dữ liệu Perp/HIP-3: {e}")

    # === 2. BỔ SUNG QUÉT ĐỒNG THỜI HOÀN TOÀN TỪ ALLMIDS (FALLBACK) ===
    try:
        mids_resp = requests.post(url, headers=headers, json={"type": "allMids"}, timeout=10).json()
        if isinstance(mids_resp, dict):
            for k, v in mids_resp.items():
                if v:
                    prices[k.upper()] = float(v)
    except Exception as e:
        print(f"Lỗi fallback allMids: {e}")

    return prices, funding_dict

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

def build_check_message(prices, funding_rates):
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường - Hyperliquid*\n🕐 `{now}`\n"]

    # === HỆ THỐNG TRA CỨU CHUỖI TOÀN DIỆN ===
    lines.append("🔍 *Danh sách các mã tìm thấy trên sàn:*")
    found_any = False
    
    # Duyệt tìm bất kỳ key nào chứa ký tự dầu khí hoặc có mức giá thực tế từ 65 đến 75
    for k, v in prices.items():
        price_val = float(v)
        if any(w in k for w in ["WTI", "BRENT", "OIL"]) or (65 <= price_val <= 75):
            lines.append(f"  • Key: `{k}` ➔ Giá thực tế: `${price_val:.4f}`")
            found_any = True
            
    if not found_any:
        lines.append("  ⚠️ Không tìm thấy kết quả phù hợp nào.")
        
    lines.append("\n─────────────────────")
    
    # ... (Giữ nguyên logic tính toán cặp spread CONFIG_PAIRS phía dưới của bạn)

    # === PHẦN TÍNH SPREAD NHƯ CŨ ===
    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a = cfg["symbol_a"]
        sym_b = cfg["symbol_b"]
        name_a = cfg["name_a"]
        name_b = cfg["name_b"]

        # Thử tìm kiếm thông minh nếu cấu hình cứng không có trong prices
        if sym_a not in prices:
            for k in prices.keys():
                if cfg["symbol_a"].upper() in k.upper():
                    sym_a = k
                    break
        if sym_b not in prices:
            for k in prices.keys():
                if cfg["symbol_b"].upper() in k.upper():
                    sym_b = k
                    break

        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        if price_a == 0 or price_b == 0:
            lines.append(f"❌ *{name_a} vs {name_b}*: Chưa cấu hình đúng mã.\n➔ Vui lòng xem danh sách quét phía trên để lấy Key chính xác điền vào CONFIG.")
            continue

        spread = price_a - price_b
        mean = cfg["mean"]
        std = cfg["std"]
        z_score = calculate_z_score(spread, mean, std)

        use_zscore = cfg.get("use_zscore", False)
        long_z = cfg.get("long_z_threshold", -1.0)
        short_z = cfg.get("short_z_threshold", 0.8)
        vol = cfg["vol_per_leg"]

        if use_zscore:
            status = "🟢 ĐẠT NGƯỠNG LONG" if z_score <= long_z else "🔴 ĐẠT NGƯỠNG SHORT" if z_score >= short_z else "⏳ Trong vùng trung tính"
            dist_to_long = f"{z_score - long_z:+.2f}"
            dist_to_short = f"{short_z - z_score:+.2f}"
        else:
            status = "⏳ Trong vùng trung tính"
            dist_to_long = "0.00"
            dist_to_short = "0.00"

        fa = funding_rates.get(sym_a, 0)
        fb = funding_rates.get(sym_b, 0)
        f_long, apr_long = calc_net_funding(fa, fb, True, vol)
        f_short, apr_short = calc_net_funding(fa, fb, False, vol)

        def fmt_f(usd, apr):
            icon = "✅" if usd >= 0 else "🔴"
            return f"{icon} `{usd:+.2f}/ngày` (APR `{apr:+.1f}%`)"

        block = (
            f"🛢 *{name_a} vs {name_b}*\n"
            f"  Mã A (`{sym_a}`): `${price_a:.4f}`\n"
            f"  Mã B (`{sym_b}`): `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.2f}` | **Z-Score: `{z_score:+.2f}`**\n\n"
            f"  📍 *Trạng thái:* {status}\n"
            f"  💸 *Funding*:\n"
            f"  Long A + Short B: {fmt_f(f_long, apr_long)}\n"
            f"  Short A + Long B: {fmt_f(f_short, apr_short)}\n"
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
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang kết nối API sàn...")
            prices, funding = get_hyperliquid_data()
            reply = build_check_message(prices, funding)
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, reply)
        except Exception as e:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: {str(e)}")

    elif text.startswith("/help"):
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "🤖 Bot Spread Trading\n/check - Snapshot + Z-Score")

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

# NOTE:
# This version only fixes midPx parsing. To fully support HIP-3 WTI/BRENTOIL,
# replace get_hyperliquid_data() with logic that enumerates all dexes and calls
# metaAndAssetCtxs/allMids for each dex, merging results by coin name.
