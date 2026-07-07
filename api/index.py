import os
import requests
from flask import Flask, request

app = Flask(__name__)

# ==================== CONFIG ====================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "XYZ:CL",
        "name_b": "Brent (B)",
        "symbol_b": "XYZ:BRENTOIL",

        # === THAM SỐ THỐNG KÊ REALISTIC (dựa trên backtest M15 3 tháng) ===
        "mean": -3.80,               # Mean Spread (CL - BRENT) thực tế ~ -3.80 đến -3.87
        "std": 1.72,                 # Std realistic theo dữ liệu M15 (trước đó dùng 0.65 là quá thấp)

        "use_zscore": True,

        # Khuyến nghị dùng ngưỡng Z-score cân bằng
        "long_z_threshold": -1.4,   # ~ -1.85 Z (khoảng P8-P10, tín hiệu khá mạnh)
        "short_z_threshold": 1.4,   # ~ +1.85 Z (khoảng P90-P92)
        "exit_z_threshold": 0.30,    # Thoát khi về gần vùng trung vị

        # === QUẢN TRỊ RỦI RO & VỐN ===
        "vol_per_leg": 14000,        # Quy mô vốn $14,000/leg
        "avg_hold_hours": 16.5,      # Thời gian giữ lệnh trung bình (dùng để tính Expected PnL)
        "fee_bps": 0.00022,          # Maker fee 2.2 bps (trade.xyz)
        "min_net_pnl": 20,           # Expected Funding PnL tối thiểu trong 1 lệnh
        "max_funding_loss": 70,      # Chấp nhận lỗ Funding tối đa $45/ngày
    }
}

# CẤU HÌNH THÔNG TIN TELEGRAM BẮN ALERT CHỦ ĐỘNG
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") 

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

    try:
        mids_resp = requests.post(url, headers=headers, json={"type": "allMids", "dex": "xyz"}, timeout=10).json()
        if isinstance(mids_resp, dict):
            prices = {k.upper(): float(v) for k, v in mids_resp.items() if v}
    except Exception as e:
        print(f"Lỗi lấy dữ liệu allMids từ DEX xyz: {e}")

    try:
        meta_resp = requests.post(url, headers=headers, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=10).json()
        if isinstance(meta_resp, list) and len(meta_resp) >= 2:
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]
            for i, asset in enumerate(universe):
                coin_name = asset.get("name", "")
                if i < len(asset_ctxs) and coin_name:
                    funding = float(asset_ctxs[i].get("funding", 0))
                    funding_dict[coin_name.upper()] = funding
                    if not coin_name.upper().startswith("XYZ:"):
                        funding_dict[f"XYZ:{coin_name.upper()}"] = funding
    except Exception as e:
        print(f"Lỗi lấy funding rate từ DEX xyz: {e}")

    return prices, funding_dict

def calc_net_funding(funding_a, funding_b, is_long_a, vol):
    net_rate = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    return net_rate * 24 * vol, net_rate * 24 * 365 * 100

def send_telegram_message(token, chat_id, text):
    if not token or not chat_id:
        print(f"[warning] Thiếu Token hoặc Chat ID. Nội dung: {text}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

def build_check_message(prices, funding_rates):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường - Hyperliquid (DEX: xyz)*\n🕐 `{now}`\n"]

    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a = cfg["symbol_a"].upper()
        sym_b = cfg["symbol_b"].upper()

        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        if price_a == 0 and sym_a.startswith("XYZ:"): price_a = float(prices.get(sym_a.split(":")[1], 0))
        if price_b == 0 and sym_b.startswith("XYZ:"): price_b = float(prices.get(sym_b.split(":")[1], 0))

        if price_a == 0 or price_b == 0:
            lines.append(f"❌ *{cfg['name_a']} vs {cfg['name_b']}*: Không lấy được giá từ DEX xyz\n")
            continue

        spread = price_a - price_b
        z_score = calculate_z_score(spread, cfg["mean"], cfg["std"])

        if z_score <= cfg["long_z_threshold"]: status = "🟢 ĐẠT NGƯỠNG LONG"
        elif z_score >= cfg["short_z_threshold"]: status = "🔴 ĐẠT NGƯỠNG SHORT"
        else: status = "⏳ Trong vùng trung tính"

        fa = funding_rates.get(sym_a, funding_rates.get(sym_a.replace("XYZ:", ""), 0))
        fb = funding_rates.get(sym_b, funding_rates.get(sym_b.replace("XYZ:", ""), 0))
        f_long, apr_long = calc_net_funding(fa, fb, True, cfg["vol_per_leg"])
        f_short, apr_short = calc_net_funding(fa, fb, False, cfg["vol_per_leg"])

        def fmt_f(usd, apr):
            return f"{'✅' if usd >= 0 else '🔴'} `{usd:+.2f}/ngày` (APR `{apr:+.1f}%`)"

        block = (
            f"─────────────────────\n"
            f"🛢 *{cfg['name_a']} vs {cfg['name_b']}*\n"
            f"  Giá WTI (`{sym_a}`): `${price_a:.4f}`\n"
            f"  Giá Brent (`{sym_b}`): `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.2f}`\n"
            f"  **Z-Score: `{z_score:+.2f}`**\n"
            f"  Mean: `{cfg['mean']}` | Std: `{cfg['std']}`\n\n"
            f"  📍 *Trạng thái:* {status}\n"
            f"  💸 *Funding* (vốn `${cfg['vol_per_leg']:,}/leg`):\n"
            f"  Long A + Short B: {fmt_f(f_long, apr_long)}\n"
            f"  Short A + Long B: {fmt_f(f_short, apr_short)}\n"
        )
        lines.append(block)

    lines.append("─────────────────────\n💡 Dùng `/check` để cập nhật lại")
    return "\n".join(lines)

# ==================== API ENDPOINTS ====================

@app.route("/api", methods=["POST", "GET"])
@app.route("/api/", methods=["POST", "GET"])  # Chống lỗi 404 khi cronjob tự động thêm dấu / ở cuối
@app.route("/api", methods=["POST", "GET"])
@app.route("/api/", methods=["POST", "GET"])
def scan_bot():
    try:
        prices, funding_rates = get_hyperliquid_data()

        for pair_key, cfg in CONFIG_PAIRS.items():
            sym_a = cfg["symbol_a"].upper()
            sym_b = cfg["symbol_b"].upper()

            price_a = float(prices.get(sym_a, 0))
            price_b = float(prices.get(sym_b, 0))

            if price_a == 0 and sym_a.startswith("XYZ:"):
                price_a = float(prices.get(sym_a.split(":")[1], 0))
            if price_b == 0 and sym_b.startswith("XYZ:"):
                price_b = float(prices.get(sym_b.split(":")[1], 0))

            if price_a == 0 or price_b == 0:
                continue

            spread = price_a - price_b
            z_score = calculate_z_score(spread, cfg["mean"], cfg["std"])

            fa = funding_rates.get(sym_a, funding_rates.get(sym_a.replace("XYZ:", ""), 0))
            fb = funding_rates.get(sym_b, funding_rates.get(sym_b.replace("XYZ:", ""), 0))

            # Xác định hướng
            if z_score <= cfg["long_z_threshold"]:
                side = "LONG"
                direction = f"🟢 *VÀO LỆNH LONG SPREAD (M15)*\n➔ BUY {cfg['name_a']} & SELL {cfg['name_b']}"
                net_usd_day, net_apr = calc_net_funding(fa, fb, True, cfg["vol_per_leg"])
            elif z_score >= cfg["short_z_threshold"]:
                side = "SHORT"
                direction = f"🔴 *VÀO LỆNH SHORT SPREAD (M15)*\n➔ SELL {cfg['name_a']} & BUY {cfg['name_b']}"
                net_usd_day, net_apr = calc_net_funding(fa, fb, False, cfg["vol_per_leg"])
            else:
                continue  # Chưa đạt ngưỡng Z-score

            # Tính expected PnL
            avg_hold = cfg.get("avg_hold_hours", 16.5)
            expected_funding_pnl = net_usd_day * (avg_hold / 24)
            min_pnl_req = cfg.get("min_net_pnl", 0)
            max_loss = cfg.get("max_funding_loss", 999)

            funding_ok = (net_usd_day >= 0) or (abs(net_usd_day) <= max_loss)
            pnl_ok = expected_funding_pnl >= min_pnl_req

            now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

            if funding_ok and pnl_ok:
                # === TRƯỜNG HỢP ĐẠT → GỬI TÍN HIỆU ===
                funding_status_str = (
                    f"✅ Nhận: `{net_usd_day:+.2f}$/ngày`"
                    if net_usd_day >= 0 else
                    f"⚠️ Trả: `{net_usd_day:+.2f}$/ngày` (Trong ngưỡng cho phép)"
                )

                alert_msg = (
                    f"🚨 *TÍN HIỆU MEAN REVERSION KHUNG M15!*\n\n"
                    f"🕐 `{now}`\n\n"
                    f"🥇 *{cfg['name_a']} vs {cfg['name_b']}*\n"
                    f"Spread: `${spread:+.2f}`\n"
                    f"**Z-Score: `{z_score:+.2f}`** (Ngưỡng: {cfg['long_z_threshold']} / {cfg['short_z_threshold']})\n\n"
                    f"{direction}\n\n"
                    f"💸 *Funding & PnL (Vốn ${cfg['vol_per_leg']:,}/leg)*\n"
                    f"• {funding_status_str}\n"
                    f"• Expected Funding PnL ({avg_hold}h): `{expected_funding_pnl:+.2f}$`\n"
                    f"• APR: `{net_apr:+.1f}%`\n\n"
                    f"✅ Đã vượt qua Spread + Funding/PnL filter"
                )
                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, alert_msg)

            else:
                # === TRƯỜNG HỢP BỊ LỌC → VẪN GỬI TELEGRAM ===
                reasons = []
                if not funding_ok:
                    reasons.append(f"Funding quá tiêu cực ({net_usd_day:+.2f}$/ngày > ±{max_loss}$)")
                if not pnl_ok:
                    reasons.append(f"Expected PnL thấp ({expected_funding_pnl:+.2f}$ < {min_pnl_req}$)")

                skip_msg = (
                    f"⚠️ *BỎ QUA TÍN HIỆU (M15)*\n\n"
                    f"🕐 `{now}`\n\n"
                    f"🛢 *{cfg['name_a']} vs {cfg['name_b']}*\n"
                    f"Spread: `${spread:+.2f}`\n"
                    f"Z-Score: `{z_score:+.2f}`\n"
                    f"Hướng: {side}\n\n"
                    f"❌ *Lý do bỏ qua:*\n• " + "\n• ".join(reasons) + "\n\n"
                    f"💡 Funding: `{net_usd_day:+.2f}$/ngày` | Expected PnL: `{expected_funding_pnl:+.2f}$`"
                )
                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, skip_msg)

        return {"status": "success", "message": "Scan completed"}, 200

    except Exception as e:
        print(f"[ERROR] scan_bot: {str(e)}")
        return {"status": "error", "message": str(e)}, 500

@app.route("/api/webhook", methods=["POST"])  # Cập nhật /api/webhook để chạy chuẩn xác trên Vercel
def telegram_webhook():
    update = request.get_json(silent=True)
    if not update: return "OK", 200
    msg = update.get("message") or update.get("edited_message")
    if not msg: return "OK", 200

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

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)