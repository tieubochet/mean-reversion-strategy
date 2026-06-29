import os
import requests
from flask import Flask, request

app = Flask(__name__)

# ==================== CONFIG ====================
# Điền chính xác mã token thuộc DEX "xyz" theo chuẩn tài liệu HIP-3
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "xyz:WTIOIL",         # ← Tên chính xác trên sàn phụ xyz
        "name_b": "Brent (B)",
        "symbol_b": "xyz:BRENTOIL",      # ← Tên chính xác trên sàn phụ xyz
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

    # Gọi API allMids kèm tham số truyền vào dex là "xyz"
    try:
        mids_resp = requests.post(
            url, headers=headers, json={"type": "allMids", "dex": "xyz"}, timeout=10
        ).json()
        if isinstance(mids_resp, dict):
            prices = {k.upper(): float(v) for k, v in mids_resp.items() if v}
    except Exception as e:
        print(f"Lỗi lấy dữ liệu allMids từ DEX xyz: {e}")

    # Gọi API metaAndAssetCtxs kèm tham số truyền vào dex là "xyz" để lấy funding rate
    try:
        meta_resp = requests.post(
            url, headers=headers, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=10
        ).json()

        if isinstance(meta_resp, list) and len(meta_resp) >= 2:
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]

            for i, asset in enumerate(universe):
                coin_name = asset.get("name", "")
                if i < len(asset_ctxs) and coin_name:
                    funding = float(asset_ctxs[i].get("funding", 0))
                    # Token trên sàn phụ xyz trả về dưới dạng "xyz:WTIOIL" hoặc "WTIOIL" tùy cấu trúc, ta lưu cả 2
                    funding_dict[coin_name.upper()] = funding
                    if not coin_name.upper().startswith("XYZ:"):
                        funding_dict[f"XYZ:{coin_name.upper()}"] = funding
    except Exception as e:
        print(f"Lỗi lấy funding rate từ DEX xyz: {e}")

    return prices, funding_dict

def calc_net_funding(funding_a, funding_b, is_long_a, vol):
    net_rate = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    return net_rate * 24 * vol, net_rate * 24 * 365 * 100

def build_check_message(prices, funding_rates):
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường - Hyperliquid (DEX: xyz)*\n🕐 `{now}`\n"]

    # === IN TOÀN BỘ DANH SÁCH MÃ ĐỂ TIỀN TRẠM CHÍNH XÁC ===
    lines.append("🔍 *Tất cả các mã đang active trên DEX xyz:*")
    all_keys = sorted(list(prices.keys()))
    
    # Chia danh sách thành các cụm nhỏ để tránh bị Telegram nuốt tin nhắn do quá dài
    chunk_size = 15
    for i in range(0, len(all_keys), chunk_size):
        chunk = all_keys[i:i+chunk_size]
        lines.append(f"  • " + ", ".join([f"`{k}`" for k in chunk]))
        
    lines.append("\n─────────────────────")
    return "\n".join(lines)

    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a = cfg["symbol_a"].upper()
        sym_b = cfg["symbol_b"].upper()
        name_a = cfg["name_a"]
        name_b = cfg["name_b"]

        # Tra cứu trực tiếp từ map prices thu được của phân vùng DEX xyz
        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        # Fallback phòng trường hợp key trả về không chứa tiền tố "xyz:"
        if price_a == 0 and sym_a.startswith("XYZ:"):
            price_a = float(prices.get(sym_a.split(":")[1], 0))
        if price_b == 0 and sym_b.startswith("XYZ:"):
            price_b = float(prices.get(sym_b.split(":")[1], 0))

        if price_a == 0 or price_b == 0:
            lines.append(f"❌ *{name_a} vs {name_b}*: Không lấy được giá từ DEX xyz\n(Mã tìm kiếm: `{sym_a}` / `{sym_b}`)\n")
            # In thử một vài mã có trong dữ liệu để debug nhanh
            lines.append("💡 Các mã hiện có trên DEX này: " + ", ".join(list(prices.keys())[:5]))
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
            if z_score <= long_z:
                status = "🟢 ĐẠT NGƯỠNG LONG"
            elif z_score >= short_z:
                status = "🔴 ĐẠT NGƯỠNG SHORT"
            else:
                status = "⏳ Trong vùng trung tính"
            dist_to_long = f"{z_score - long_z:+.2f}"
            dist_to_short = f"{short_z - z_score:+.2f}"
        else:
            status = "⏳ Trong vùng trung tính"
            dist_to_long, dist_to_short = "0.00", "0.00"

        fa = funding_rates.get(sym_a, funding_rates.get(sym_a.replace("XYZ:", ""), 0))
        fb = funding_rates.get(sym_b, funding_rates.get(sym_b.replace("XYZ:", ""), 0))
        f_long, apr_long = calc_net_funding(fa, fb, True, vol)
        f_short, apr_short = calc_net_funding(fa, fb, False, vol)

        def fmt_f(usd, apr):
            icon = "✅" if usd >= 0 else "🔴"
            sign = "+" if usd >= 0 else ""
            return f"{icon} `{sign}${usd:.2f}/ngày` (APR `{apr:+.1f}%`)"

        block = (
            f"─────────────────────\n"
            f"🛢 *{name_a} vs {name_b}*\n"
            f"  Giá WTI (`{sym_a}`): `${price_a:.4f}`\n"
            f"  Giá Brent (`{sym_b}`): `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.2f}`\n"
            f"  **Z-Score: `{z_score:+.2f}`**\n"
            f"  Mean: `{mean}` | Std: `{std}`\n\n"
            f"  📍 *Trạng thái:* {status}\n"
            f"  ↳ Cách ngưỡng Long : `{dist_to_long}`\n"
            f"  ↳ Cách ngưỡng Short: `{dist_to_short}`\n\n"
            f"  💸 *Funding* (vốn `${vol:,}/leg`):\n"
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
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang kết nối phân vùng DEX xyz trên HyperCore...")
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