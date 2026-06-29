import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# CẤU HÌNH CÁC CẶP SPREAD
# ============================================================
# avg_hold_hours  : thời gian giữ lệnh trung bình từ backtest (dùng để ước tính
#                   tổng funding cost trong 1 trade)
# fee_bps         : phí maker mỗi leg (basis points). 2.2bps = 0.00022
#                   Tổng phí 1 vòng = vol_per_leg * 2 legs * fee_bps * 2 (open+close)
# ============================================================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "WTI",
        "name_b": "Brent (B)",
        "symbol_b": "BRENT",
        "mean": -3.69,
        "std": 2.52,
        "long_threshold": -6.84,   # P10 – ngưỡng Long
        "short_threshold": -0.78,  # P90 – ngưỡng Short
        "vol_per_leg": 700,        # USD/leg (tổng vốn $1,400)
        "avg_hold_hours": 69,      # từ backtest row L<-3.7
        "fee_bps": 0.00022,        # maker fee 2.2bps
    }
}


# ============================================================
# LẤY DỮ LIỆU HYPERLIQUID
# ============================================================
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
                # HL funding = rate per 1 giờ, dạng thập phân
                funding_dict[name] = float(asset_ctxs[i].get("funding", 0))

    return prices, funding_dict


# ============================================================
# TÍNH FUNDING NET (đúng chiều trade)
# ============================================================
def calc_net_funding(funding_a, funding_b, is_long_a: bool, vol_per_leg: float):
    """
    Quy ước HL: funding > 0 → Long TRẢ cho Short; funding < 0 → Short TRẢ cho Long

    Long A + Short B:
      Leg A (Long) : trả funding_a  → P&L = -funding_a × vol
      Leg B (Short): nhận funding_b → P&L = +funding_b × vol
      Net rate/h = funding_b - funding_a

    Short A + Long B: đảo dấu
      Net rate/h = funding_a - funding_b
    """
    if is_long_a:
        net_rate_per_hour = funding_b - funding_a
    else:
        net_rate_per_hour = funding_a - funding_b

    net_usd_per_day = net_rate_per_hour * 24 * vol_per_leg
    net_apr_pct = net_rate_per_hour * 24 * 365 * 100   # % năm trên 1 leg

    return net_usd_per_day, net_apr_pct


# ============================================================
# TÍNH PNL ƯỚC TÍNH & QUYẾT ĐỊNH GỬI SIGNAL
# ============================================================
def evaluate_signal(config, current_spread, is_long_a, net_usd_day, price_a):
    """
    Trả về dict chứa các chỉ số PnL, hoặc None nếu không đáng trade.

    Gross spread PnL = khoảng cách spread → mean × số units
    Funding cost tổng = net_usd_day × (avg_hold_hours / 24)
      (âm = phải trả, dương = nhận)
    Phí giao dịch = vol_per_leg × 2 legs × fee_bps × 2 (open + close)
    Net PnL ước tính = gross_spread_pnl + funding_net_total - fee_total
    """
    vol = config["vol_per_leg"]
    mean = config["mean"]
    avg_hold_days = config["avg_hold_hours"] / 24
    fee_bps = config["fee_bps"]

    # Số units (barrel/oz/...) xấp xỉ theo giá asset A
    units = vol / price_a if price_a > 0 else 0

    # Gross PnL từ spread revert về mean
    # Long A+Short B: lãi khi spread tăng (bớt âm) về mean
    spread_to_mean = mean - current_spread if is_long_a else current_spread - mean
    gross_spread_pnl = spread_to_mean * units

    # Tổng funding trong thời gian giữ lệnh (có thể âm hoặc dương)
    funding_net_total = net_usd_day * avg_hold_days

    # Phí maker cả 2 chiều (open + close), 2 legs
    fee_total = vol * 2 * fee_bps * 2

    # Net PnL ước tính
    net_pnl = gross_spread_pnl + funding_net_total - fee_total

    return {
        "units": units,
        "gross_spread_pnl": gross_spread_pnl,
        "funding_net_total": funding_net_total,
        "fee_total": fee_total,
        "net_pnl": net_pnl,
        "avg_hold_days": avg_hold_days,
    }


# ============================================================
# BUILD MESSAGE TELEGRAM
# ============================================================
def build_message(config, current_spread, signal_direction, net_usd_day,
                  net_apr_pct, ev: dict):
    name_a = config["name_a"]
    name_b = config["name_b"]
    long_t = config["long_threshold"]
    short_t = config["short_threshold"]
    vol = config["vol_per_leg"]
    units = ev["units"]

    # Emoji theo trạng thái funding
    if net_usd_day >= 0:
        funding_icon = "✅"
        funding_note = "có lợi"
    else:
        funding_icon = "🟡"
        funding_note = "bất lợi nhưng vẫn net lãi"

    sign_f = "+" if net_usd_day >= 0 else ""
    sign_n = "+" if ev["net_pnl"] >= 0 else ""

    msg = (
        f"🚨 *Signal Adaptive mới!*\n\n"
        f"🥇 *{name_a} vs {name_b}*\n"
        f"Spread: `${current_spread:+.2f}` (L<`{long_t}` / S>`{short_t}`)\n"
        f"➔ {signal_direction}\n\n"
        f"{funding_icon} Funding `{sign_f}${net_usd_day:.2f}/ngày` ({funding_note})\n"
        f"   net APR `{net_apr_pct:+.1f}%` · vốn `${vol:,}/leg` · `~{units:.3f} units`\n\n"
        f"📐 *Ước tính 1 trade* (hold ~`{config['avg_hold_hours']}h`):\n"
        f"  • Gross spread PnL : `${ev['gross_spread_pnl']:+.2f}`\n"
        f"  • Funding ({ev['avg_hold_days']:.1f} ngày): `${ev['funding_net_total']:+.2f}`\n"
        f"  • Phí maker (x4)  : `-${ev['fee_total']:.2f}`\n"
        f"  • *Net PnL ước tính : `{sign_n}${ev['net_pnl']:.2f}`*\n\n"
        f"📊 Mean `{config['mean']}` | Std `{config['std']}`"
    )
    return msg


# ============================================================
# TELEGRAM HELPER
# ============================================================
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
# BUILD MESSAGE /check
# ============================================================
def build_check_message(prices, funding_rates):
    """
    Trả về snapshot toàn bộ cặp: giá, spread, funding, trạng thái ngưỡng.
    Hiển thị cả 2 chiều funding (Long A/Short B và Short A/Long B) để tiện so sánh.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    lines = [f"📊 *Snapshot thị trường*\n🕐 `{now}`\n"]

    for pair_key, cfg in CONFIG_PAIRS.items():
        sym_a, sym_b = cfg["symbol_a"], cfg["symbol_b"]
        name_a, name_b = cfg["name_a"], cfg["name_b"]

        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        if price_a == 0 or price_b == 0:
            lines.append(f"❌ *{name_a}/{name_b}*: Không lấy được giá\n")
            continue

        spread = price_a - price_b
        mean   = cfg["mean"]
        long_t = cfg["long_threshold"]
        short_t = cfg["short_threshold"]
        vol    = cfg["vol_per_leg"]

        # Khoảng cách đến ngưỡng
        dist_to_long  = spread - long_t   # âm = đã vượt ngưỡng Long
        dist_to_short = short_t - spread  # âm = đã vượt ngưỡng Short

        # Trạng thái spread
        if spread <= long_t:
            spread_status = "🟢 ĐẠT ngưỡng LONG"
        elif spread >= short_t:
            spread_status = "🔴 ĐẠT ngưỡng SHORT"
        else:
            spread_status = "⏳ Trong vùng trung tính"

        # Funding cả 2 chiều để người dùng nắm hết
        fa = funding_rates.get(sym_a, 0)
        fb = funding_rates.get(sym_b, 0)
        # Long A + Short B
        f_long_a_day,  f_long_a_apr  = calc_net_funding(fa, fb, True,  vol)
        # Short A + Long B
        f_short_a_day, f_short_a_apr = calc_net_funding(fa, fb, False, vol)

        def fmt_f(usd, apr):
            icon = "✅" if usd >= 0 else "🔴"
            sign = "+" if usd >= 0 else ""
            return f"{icon} `{sign}${usd:.3f}/ngày` (APR `{apr:+.1f}%`)"

        block = (
            f"─────────────────────\n"
            f"🛢 *{name_a} vs {name_b}*\n"
            f"  Giá {sym_a}: `${price_a:.4f}`\n"
            f"  Giá {sym_b}: `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.4f}`\n"
            f"  Mean: `{mean}` | Std: `{cfg['std']}`\n"
            f"  Ngưỡng: Long ≤ `{long_t}` | Short ≥ `{short_t}`\n"
            f"\n"
            f"  📍 *Trạng thái:* {spread_status}\n"
            f"  ↳ Cách ngưỡng Long : `{dist_to_long:+.4f}`\n"
            f"  ↳ Cách ngưỡng Short: `{dist_to_short:+.4f}`\n"
            f"\n"
            f"  💸 *Funding* (vốn `${vol:,}/leg`):\n"
            f"  Long {sym_a}+Short {sym_b}: {fmt_f(f_long_a_day, f_long_a_apr)}\n"
            f"  Short {sym_a}+Long {sym_b}: {fmt_f(f_short_a_day, f_short_a_apr)}\n"
        )
        lines.append(block)

    lines.append("─────────────────────\n💡 Dùng /check để cập nhật lại")
    return "\n".join(lines)


# ============================================================
# API ENDPOINT
# ============================================================
@app.route("/api", methods=["GET", "POST"])
def scan_bot():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        CRON_SECRET = os.environ.get("CRON_SECRET")
        if CRON_SECRET and data.get("secret") != CRON_SECRET:
            return "Unauthorized – Sai Secret Key", 401

        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return "Thiếu biến môi trường TELEGRAM", 500

        try:
            prices, funding_rates = get_hyperliquid_data()

            for pair_key, config in CONFIG_PAIRS.items():
                sym_a, sym_b = config["symbol_a"], config["symbol_b"]

                price_a = float(prices.get(sym_a, 0))
                price_b = float(prices.get(sym_b, 0))

                print(f"--- Quét {pair_key} | {sym_a}: {price_a} | {sym_b}: {price_b}")

                if price_a == 0 or price_b == 0:
                    print(f"❌ Không lấy được giá {sym_a} hoặc {sym_b}")
                    continue

                current_spread = price_a - price_b
                print(f"Spread: {current_spread:.4f}")

                # Xác định chiều trade
                is_long_a = None
                signal_direction = ""
                if current_spread <= config["long_threshold"]:
                    is_long_a = True
                    signal_direction = f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                elif current_spread >= config["short_threshold"]:
                    is_long_a = False
                    signal_direction = f"🔴 Short {config['name_a']} + Long {config['name_b']}"

                if is_long_a is None:
                    print("⏳ Spread bình thường, chưa đạt ngưỡng.")
                    continue

                # Tính funding đúng chiều
                funding_a = funding_rates.get(sym_a, 0)
                funding_b = funding_rates.get(sym_b, 0)
                net_usd_day, net_apr_pct = calc_net_funding(
                    funding_a, funding_b, is_long_a, config["vol_per_leg"]
                )
                print(f"Funding net: ${net_usd_day:.4f}/ngày | APR: {net_apr_pct:.2f}%")

                # Đánh giá có đáng trade không
                ev = evaluate_signal(config, current_spread, is_long_a, net_usd_day, price_a)
                print(f"Net PnL ước tính: ${ev['net_pnl']:.4f}")

                if ev["net_pnl"] <= 0:
                    print(
                        f"🚫 Net PnL âm (${ev['net_pnl']:.4f}) – "
                        f"funding bất lợi vượt lợi nhuận spread. Bỏ qua."
                    )
                    continue

                # Build & gửi
                msg = build_message(
                    config, current_spread, signal_direction,
                    net_usd_day, net_apr_pct, ev
                )
                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                print("🚀 Đã gửi signal về Telegram!")

            return {"status": "success"}, 200

        except Exception as e:
            print(f"Lỗi hệ thống: {e}")
            return str(e), 500

    return "Bot đang hoạt động. Dùng POST để trigger."


# ============================================================
# WEBHOOK – nhận lệnh từ Telegram
# ============================================================
# Cách đăng ký webhook (chạy 1 lần sau khi deploy):
#   https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<domain>/webhook
# ============================================================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        return "Thiếu TELEGRAM_BOT_TOKEN", 500

    update = request.get_json(silent=True)
    if not update:
        return "OK", 200

    # Hỗ trợ cả message thường và edited_message
    msg_obj = update.get("message") or update.get("edited_message")
    if not msg_obj:
        return "OK", 200

    chat_id = str(msg_obj["chat"]["id"])
    text_in = msg_obj.get("text", "").strip().lower()

    # Chỉ phản hồi đúng chat_id đã cấu hình (bảo mật)
    ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        return "OK", 200

    # ── /check ──
    if text_in.startswith("/check"):
        try:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                  "⏳ Đang lấy dữ liệu từ Hyperliquid...")
            prices, funding_rates = get_hyperliquid_data()
            reply = build_check_message(prices, funding_rates)
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, reply)
        except Exception as e:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                  f"❌ Lỗi khi lấy dữ liệu: `{e}`")

    # ── /help ──
    elif text_in.startswith("/help"):
        help_msg = (
            "🤖 *Bot Spread Trading – Danh sách lệnh*\n\n"
            "/check – Xem snapshot spread & funding hiện tại\n"
            "/help  – Hiển thị menu này"
        )
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, help_msg)

    return "OK", 200