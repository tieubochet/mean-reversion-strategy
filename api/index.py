import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# CẤU HÌNH CÁC CẶP SPREAD
# ============================================================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "WTI",
        "name_b": "Brent (B)",
        "symbol_b": "BRENT",
        "mean": -3.69,
        "std": 2.52,

        # === Khuyến nghị: Dùng Z-Score ===
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


# ============================================================
# HÀM HỖ TRỢ
# ============================================================
def calculate_z_score(spread, mean, std):
    if std == 0:
        return 0
    return (spread - mean) / std


def get_hyperliquid_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}

    prices = requests.post(
        url, headers=headers, json={"type": "allMids"}, timeout=10
    ).json()

    funding_resp = requests.post(
        url, headers=headers, json={"type": "metaAndAssetCtxs"}, timeout=10
    ).json()

    funding_dict = {}
    if isinstance(funding_resp, list) and len(funding_resp) > 1:
        universe = funding_resp[0].get("universe", [])
        asset_ctxs = funding_resp[1]
        for i, asset in enumerate(universe):
            name = asset.get("name")
            if i < len(asset_ctxs):
                funding_dict[name] = float(asset_ctxs[i].get("funding", 0))

    return prices, funding_dict


def calc_net_funding(funding_a, funding_b, is_long_a: bool, vol_per_leg: float):
    if is_long_a:
        net_rate_per_hour = funding_b - funding_a
    else:
        net_rate_per_hour = funding_a - funding_b

    net_usd_per_day = net_rate_per_hour * 24 * vol_per_leg
    net_apr_pct = net_rate_per_hour * 24 * 365 * 100
    return net_usd_per_day, net_apr_pct


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

    gross_spread_pnl = spread_to_mean * units
    funding_net_total = net_usd_day * avg_hold_days
    fee_total = vol * 2 * fee_bps * 2
    net_pnl = gross_spread_pnl + funding_net_total - fee_total

    min_net_pnl = config.get("min_net_pnl", 0)
    max_funding_loss = config.get("max_funding_loss", 50)

    if net_pnl >= min_net_pnl:
        quality = "good"
        should_send = True
        recommendation = "✅ Signal tốt - Nên vào lệnh"
    elif funding_net_total >= -max_funding_loss and net_pnl > -30:
        quality = "acceptable"
        should_send = True
        recommendation = "🟡 Signal chấp nhận được (funding hơi xấu)"
    else:
        quality = "poor"
        should_send = False
        recommendation = "🚫 Signal yếu - Bỏ qua"

    breakeven_spread_needed = 0
    if funding_net_total < 0 and units > 0:
        breakeven_spread_needed = abs(funding_net_total) / units

    exit_z = config.get("exit_z_threshold", 0.2)
    should_exit = abs(z_score) <= exit_z

    return {
        "units": round(units, 4),
        "z_score": round(z_score, 2),
        "gross_spread_pnl": round(gross_spread_pnl, 2),
        "funding_net_total": round(funding_net_total, 2),
        "fee_total": round(fee_total, 2),
        "net_pnl": round(net_pnl, 2),
        "avg_hold_days": round(avg_hold_days, 1),
        "should_send": should_send,
        "quality": quality,
        "recommendation": recommendation,
        "breakeven_spread_needed": round(breakeven_spread_needed, 2),
        "is_funding_negative": funding_net_total < 0,
        "should_exit": should_exit,
    }


def build_message(config, current_spread, signal_direction, net_usd_day,
                  net_apr_pct, ev: dict):
    name_a = config["name_a"]
    name_b = config["name_b"]
    vol = config["vol_per_leg"]

    if net_usd_day >= 0:
        funding_icon = "✅"
        funding_note = "có lợi"
    else:
        funding_icon = "🟡"
        funding_note = "bất lợi nhưng chấp nhận được"

    sign_f = "+" if net_usd_day >= 0 else ""
    sign_n = "+" if ev["net_pnl"] >= 0 else ""

    exit_note = ""
    if ev.get("should_exit"):
        exit_note = "\n⚠️ *Nên cân nhắc thoát lệnh* (Spread đã quay về gần Mean)"

    msg = (
        f"🚨 *Signal Adaptive mới!*\n\n"
        f"🥇 *{name_a} vs {name_b}*\n"
        f"Spread: `${current_spread:+.2f}` | Z-Score: `{ev['z_score']:+.2f}`\n"
        f"➔ {signal_direction}\n\n"
        f"{funding_icon} Funding: `{sign_f}${net_usd_day:.2f}/ngày` ({funding_note})\n"
        f"   net APR: `{net_apr_pct:+.1f}%` · Vốn: `${vol:,}/leg` · `~{ev['units']:.3f} units`\n\n"
        f"📐 *Ước tính 1 trade* (hold ~`{config['avg_hold_hours']}h`):\n"
        f"  • Gross Spread PnL : `${ev['gross_spread_pnl']:+.2f}`\n"
        f"  • Funding ({ev['avg_hold_days']:.1f} ngày) : `${ev['funding_net_total']:+.2f}`\n"
        f"  • Phí maker (x4)     : `-${ev['fee_total']:.2f}`\n"
        f"  • *Net PnL ước tính : `{sign_n}${ev['net_pnl']:.2f}`*\n\n"
        f"📊 {ev['recommendation']}"
        f"{exit_note}\n\n"
        f"Mean: `{config['mean']}` | Std: `{config['std']}`"
    )
    return msg


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"⚠️ Telegram lỗi: {resp.status_code} – {resp.text}")
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")


# ============================================================
# API ENDPOINT - SCAN & GỬI SIGNAL
# ============================================================
@app.route("/api", methods=["GET", "POST"])
def scan_bot():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        CRON_SECRET = os.environ.get("CRON_SECRET")
        if CRON_SECRET and data.get("secret") != CRON_SECRET:
            return "Unauthorized", 401

        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return "Thiếu biến môi trường TELEGRAM", 500

        try:
            prices, funding_rates = get_hyperliquid_data()

            for pair_key, config in CONFIG_PAIRS.items():
                sym_a = config["symbol_a"]
                sym_b = config["symbol_b"]
                price_a = float(prices.get(sym_a, 0))
                price_b = float(prices.get(sym_b, 0))

                if price_a == 0 or price_b == 0:
                    continue

                current_spread = price_a - price_b
                z_score = calculate_z_score(current_spread, config["mean"], config["std"])

                use_zscore = config.get("use_zscore", False)
                is_long_a = None
                signal_direction = ""

                if use_zscore:
                    long_z = config.get("long_z_threshold", -1.0)
                    short_z = config.get("short_z_threshold", 0.8)

                    if z_score <= long_z:
                        is_long_a = True
                        signal_direction = f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                    elif z_score >= short_z:
                        is_long_a = False
                        signal_direction = f"🔴 Short {config['name_a']} + Long {config['name_b']}"
                else:
                    if current_spread <= config.get("long_threshold", -3.7):
                        is_long_a = True
                        signal_direction = f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                    elif current_spread >= config.get("short_threshold", -2.8):
                        is_long_a = False
                        signal_direction = f"🔴 Short {config['name_a']} + Long {config['name_b']}"

                if is_long_a is None:
                    continue

                funding_a = funding_rates.get(sym_a, 0)
                funding_b = funding_rates.get(sym_b, 0)
                net_usd_day, net_apr_pct = calc_net_funding(
                    funding_a, funding_b, is_long_a, config["vol_per_leg"]
                )

                ev = evaluate_signal(config, current_spread, is_long_a, net_usd_day, price_a)

                print(f"[{pair_key}] Z={ev['z_score']:.2f} | Net PnL=${ev['net_pnl']:.2f} | {ev['quality']}")

                if not ev["should_send"]:
                    continue

                msg = build_message(config, current_spread, signal_direction,
                                    net_usd_day, net_apr_pct, ev)
                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                print("🚀 Đã gửi signal!")

            return {"status": "success"}, 200

        except Exception as e:
            print(f"Lỗi hệ thống: {e}")
            return str(e), 500

    return "Bot đang hoạt động. Dùng POST để trigger."


# ============================================================
# WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        return "Thiếu TELEGRAM_BOT_TOKEN", 500

    update = request.get_json(silent=True)
    if not update:
        return "OK", 200

    msg_obj = update.get("message") or update.get("edited_message")
    if not msg_obj:
        return "OK", 200

    chat_id = str(msg_obj["chat"]["id"])
    text_in = msg_obj.get("text", "").strip().lower()

    ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        return "OK", 200

    if text_in.startswith("/check"):
        try:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang lấy dữ liệu...")
            prices, funding_rates = get_hyperliquid_data()
            # Bạn có thể giữ nguyên hàm build_check_message cũ hoặc nâng cấp sau
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "Tính năng /check đang được nâng cấp...")
        except Exception as e:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: {e}")

    elif text_in.startswith("/help"):
        help_msg = "🤖 *Bot Spread Trading*\n\n/check - Snapshot thị trường\n/help - Menu này"
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, help_msg)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)