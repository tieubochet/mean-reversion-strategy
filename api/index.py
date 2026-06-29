import os
import requests
from flask import Flask, request

app = Flask(__name__)

# ==================== CONFIG ====================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "WTI",
        "name_b": "Brent (B)",
        "symbol_b": "BRENT",
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
    prices = requests.post(url, headers=headers, json={"type": "allMids"}, timeout=10).json()
    funding_resp = requests.post(url, headers=headers, json={"type": "metaAndAssetCtxs"}, timeout=10).json()

    funding_dict = {}
    if isinstance(funding_resp, list) and len(funding_resp) > 1:
        universe = funding_resp[0].get("universe", [])
        asset_ctxs = funding_resp[1]
        for i, asset in enumerate(universe):
            if i < len(asset_ctxs):
                funding_dict[asset.get("name")] = float(asset_ctxs[i].get("funding", 0))
    return prices, funding_dict

def calc_net_funding(funding_a, funding_b, is_long_a, vol):
    net_rate = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    return net_rate * 24 * vol, net_rate * 24 * 365 * 100

def evaluate_signal(config, current_spread, is_long_a, net_usd_day, price_a):
    # ... (giữ nguyên code evaluate_signal như lần trước)
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

def build_message(config, current_spread, signal_direction, net_usd_day, net_apr_pct, ev):
    # ... (giữ nguyên hàm build_message đã viết lần trước)
    name_a = config["name_a"]
    name_b = config["name_b"]
    vol = config["vol_per_leg"]

    funding_icon = "✅" if net_usd_day >= 0 else "🟡"
    funding_note = "có lợi" if net_usd_day >= 0 else "bất lợi nhưng chấp nhận được"
    sign_f = "+" if net_usd_day >= 0 else ""
    sign_n = "+" if ev["net_pnl"] >= 0 else ""
    exit_note = "\n⚠️ *Nên cân nhắc thoát lệnh*" if ev.get("should_exit") else ""

    return (
        f"🚨 *Signal Adaptive mới!*\n\n"
        f"🥇 *{name_a} vs {name_b}*\n"
        f"Spread: `${current_spread:+.2f}` | Z-Score: `{ev['z_score']:+.2f}`\n"
        f"➔ {signal_direction}\n\n"
        f"{funding_icon} Funding: `{sign_f}${net_usd_day:.2f}/ngày` ({funding_note})\n"
        f"   net APR: `{net_apr_pct:+.1f}%` · Vốn: `${vol:,}/leg`\n\n"
        f"📐 *Ước tính 1 trade*:\n"
        f"  • Gross: `${ev['gross_spread_pnl']:+.2f}`\n"
        f"  • Funding: `${ev['funding_net_total']:+.2f}`\n"
        f"  • Phí: `-${ev['fee_total']:.2f}`\n"
        f"  • *Net PnL: `{sign_n}${ev['net_pnl']:.2f}`*\n\n"
        f"📊 {ev['recommendation']}{exit_note}\n\n"
        f"Mean: `{config['mean']}` | Std: `{config['std']}`"
    )

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

def build_check_message(prices, funding_rates):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường - Hyperliquid*\n🕐 `{now}`\n"]

    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a, sym_b = cfg["symbol_a"], cfg["symbol_b"]
        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))
        if price_a == 0 or price_b == 0:
            continue

        spread = price_a - price_b
        z = calculate_z_score(spread, cfg["mean"], cfg["std"])
        use_z = cfg.get("use_zscore", False)

        if use_z:
            status = "🟢 LONG" if z <= cfg.get("long_z_threshold", -1.0) else \
                     ("🔴 SHORT" if z >= cfg.get("short_z_threshold", 0.8) else "⏳ Trung tính")
        else:
            status = "🟢 LONG" if spread <= cfg.get("long_threshold", -3.7) else \
                     ("🔴 SHORT" if spread >= cfg.get("short_threshold", -2.8) else "⏳ Trung tính")

        fa, fb = funding_rates.get(sym_a, 0), funding_rates.get(sym_b, 0)
        f1, a1 = calc_net_funding(fa, fb, True, cfg["vol_per_leg"])
        f2, a2 = calc_net_funding(fa, fb, False, cfg["vol_per_leg"])

        lines.append(
            f"─────────────────────\n"
            f"🛢 *{cfg['name_a']} vs {cfg['name_b']}*\n"
            f"Spread: `${spread:+.2f}` | **Z-Score: `{z:+.2f}`**\n"
            f"Trạng thái: {status}\n"
            f"Funding Long A: `+${f1:.2f}/ngày` | Short A: `+${f2:.2f}/ngày`\n"
        )

    lines.append("─────────────────────\n💡 Dùng `/check` để cập nhật")
    return "\n".join(lines)

# ==================== API ====================
@app.route("/api", methods=["POST"])
def scan_bot():
    # ... (giữ nguyên logic scan_bot như lần trước)
    pass  # (bạn có thể copy lại phần scan_bot từ code trước)

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
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang lấy dữ liệu...")
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