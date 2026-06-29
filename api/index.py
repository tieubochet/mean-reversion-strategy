import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ================================================================
# CONFIG_PAIRS – Thêm/bớt cặp tại đây, không cần sửa code khác
# ================================================================
#
# calibrated: True  → đã có backtest, bot gửi signal khi đạt ngưỡng
# calibrated: False → chưa có backtest, bot chỉ hiển thị trong /check
#                     để theo dõi và thu thập dữ liệu
#
# long_threshold  : spread ≤ giá trị này → Long A + Short B
# short_threshold : spread ≥ giá trị này → Short A + Long B
# mean / std      : từ backtest lịch sử (dùng để tính gross PnL ước tính)
# avg_hold_hours  : thời gian giữ lệnh trung bình từ backtest
# fee_bps         : phí maker mỗi leg (0.00022 = 2.2 bps)
# vol_per_leg     : USD vốn mỗi leg
#
# Tên symbol: dùng /symbols <từ khóa> để tìm đúng tên trên Hyperliquid
# ================================================================
CONFIG_PAIRS = {

    # ── Dầu thô ─────────────────────────────────────────────────
    "WTIOIL_BRENTOIL": {
        "label":           "Dầu WTI vs Brent",
        "symbol_a":        "WTIOIL",      # xyz:WTIOIL trên HL
        "symbol_b":        "BRENTOIL",    # xyz:BRENTOIL trên HL
        "calibrated":      True,
        "mean":            -3.69,
        "std":             2.52,
        "long_threshold":  -6.84,         # P10 backtest 115 ngày
        "short_threshold": -0.78,         # P90
        "avg_hold_hours":  69,
        "vol_per_leg":     700,
        "fee_bps":         0.00022,
    },

    # ── Vàng ────────────────────────────────────────────────────
    "XYZ_GOLD_vs_CASH_GOLD": {
        "label":           "Gold XYZ vs Cash",
        "symbol_a":        "XAUUSDT",     # ← cập nhật sau khi dùng /symbols gold
        "symbol_b":        "XAU",         # ← cập nhật sau khi dùng /symbols gold
        "calibrated":      False,         # chưa backtest, chỉ theo dõi
        "mean":            None,
        "std":             None,
        "long_threshold":  None,
        "short_threshold": None,
        "avg_hold_hours":  48,
        "vol_per_leg":     700,
        "fee_bps":         0.00022,
    },

    # ── Bạc ─────────────────────────────────────────────────────
    "XYZ_SILVER_vs_CASH_SILVER": {
        "label":           "Silver XYZ vs Cash",
        "symbol_a":        "XAGUSD",      # ← cập nhật sau khi dùng /symbols silver
        "symbol_b":        "XAG",         # ← cập nhật sau khi dùng /symbols silver
        "calibrated":      False,
        "mean":            None,
        "std":             None,
        "long_threshold":  None,
        "short_threshold": None,
        "avg_hold_hours":  48,
        "vol_per_leg":     700,
        "fee_bps":         0.00022,
    },

}


# ================================================================
# LẤY DỮ LIỆU HYPERLIQUID
# ================================================================
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
    universe_names = []

    if isinstance(funding_resp, list) and len(funding_resp) > 1:
        universe = funding_resp[0].get("universe", [])
        asset_ctxs = funding_resp[1]
        for i, asset in enumerate(universe):
            name = asset.get("name", "")
            universe_names.append(name)
            if i < len(asset_ctxs):
                funding_dict[name] = float(asset_ctxs[i].get("funding", 0))

    return prices, funding_dict, universe_names


# ================================================================
# TÍNH FUNDING NET (đúng chiều trade)
# ================================================================
def calc_net_funding(funding_a, funding_b, is_long_a: bool, vol_per_leg: float):
    """
    HL: funding > 0 → Long TRẢ cho Short

    Long A + Short B  → net/h = funding_b - funding_a
    Short A + Long B  → net/h = funding_a - funding_b
    """
    net_rate_per_hour = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    net_usd_per_day = net_rate_per_hour * 24 * vol_per_leg
    net_apr_pct = net_rate_per_hour * 24 * 365 * 100
    return net_usd_per_day, net_apr_pct


# ================================================================
# TÍNH PNL ƯỚC TÍNH
# ================================================================
def evaluate_signal(cfg, current_spread, is_long_a, net_usd_day, price_a):
    vol = cfg["vol_per_leg"]
    avg_hold_days = cfg["avg_hold_hours"] / 24
    units = vol / price_a if price_a > 0 else 0
    spread_to_mean = (cfg["mean"] - current_spread) if is_long_a else (current_spread - cfg["mean"])
    gross_pnl = spread_to_mean * units
    funding_total = net_usd_day * avg_hold_days
    fee_total = vol * 2 * cfg["fee_bps"] * 2
    return {
        "units":          units,
        "gross_pnl":      gross_pnl,
        "funding_total":  funding_total,
        "fee_total":      fee_total,
        "net_pnl":        gross_pnl + funding_total - fee_total,
        "avg_hold_days":  avg_hold_days,
    }


# ================================================================
# BUILD SIGNAL MESSAGE
# ================================================================
def build_signal_message(cfg, current_spread, direction, net_usd_day, net_apr_pct, ev):
    vol = cfg["vol_per_leg"]
    icon = "✅" if net_usd_day >= 0 else "🟡"
    note = "có lợi" if net_usd_day >= 0 else "bất lợi nhưng vẫn net lãi"
    sf = "+" if net_usd_day >= 0 else ""
    sn = "+" if ev["net_pnl"] >= 0 else ""
    return (
        f"🚨 *Signal Adaptive mới!*\n\n"
        f"🥇 *{cfg['label']}*\n"
        f"Spread: `${current_spread:+.4f}`"
        f" (L<`{cfg['long_threshold']}` / S>`{cfg['short_threshold']}`)\n"
        f"➔ {direction}\n\n"
        f"{icon} Funding `{sf}${net_usd_day:.3f}/ngày` ({note})\n"
        f"   APR `{net_apr_pct:+.1f}%` · vốn `${vol:,}/leg` · `~{ev['units']:.3f} units`\n\n"
        f"📐 *Ước tính 1 trade* (hold ~`{cfg['avg_hold_hours']}h`):\n"
        f"  • Gross spread PnL: `${ev['gross_pnl']:+.4f}`\n"
        f"  • Funding ({ev['avg_hold_days']:.1f} ngày): `${ev['funding_total']:+.4f}`\n"
        f"  • Phí maker (x4) : `-${ev['fee_total']:.4f}`\n"
        f"  • *Net PnL ước tính: `{sn}${ev['net_pnl']:.4f}`*\n\n"
        f"📊 Mean `{cfg['mean']}` | Std `{cfg['std']}`"
    )


# ================================================================
# BUILD /check MESSAGE
# ================================================================
def build_check_message(prices, funding_rates):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"📊 *Snapshot thị trường*\n🕐 `{now}`\n"]

    for key, cfg in CONFIG_PAIRS.items():
        sym_a, sym_b = cfg["symbol_a"], cfg["symbol_b"]
        price_a = float(prices.get(sym_a, 0))
        price_b = float(prices.get(sym_b, 0))

        # ── Symbol không tìm thấy ──
        if price_a == 0 or price_b == 0:
            not_found = []
            if price_a == 0: not_found.append(f"`{sym_a}`")
            if price_b == 0: not_found.append(f"`{sym_b}`")
            lines.append(
                f"─────────────────────\n"
                f"❓ *{cfg['label']}*\n"
                f"Symbol chưa đúng: {', '.join(not_found)}\n"
                f"👉 Dùng `/symbols <từ khóa>` để tìm tên chính xác\n"
            )
            continue

        spread = price_a - price_b
        vol = cfg["vol_per_leg"]
        fa = funding_rates.get(sym_a, 0)
        fb = funding_rates.get(sym_b, 0)
        f_long_day,  f_long_apr  = calc_net_funding(fa, fb, True,  vol)
        f_short_day, f_short_apr = calc_net_funding(fa, fb, False, vol)

        def fmt_f(usd, apr):
            return f"{'✅' if usd >= 0 else '🔴'} `{'+'if usd>=0 else''}${usd:.4f}/ngày` (APR `{apr:+.1f}%`)"

        # ── Trạng thái spread (chỉ khi đã calibrated) ──
        if cfg["calibrated"]:
            long_t, short_t = cfg["long_threshold"], cfg["short_threshold"]
            if spread <= long_t:
                status = "🟢 ĐẠT ngưỡng LONG"
            elif spread >= short_t:
                status = "🔴 ĐẠT ngưỡng SHORT"
            else:
                status = "⏳ Trong vùng trung tính"
            threshold_line = (
                f"  Ngưỡng: Long ≤ `{long_t}` | Short ≥ `{short_t}`\n"
                f"  Mean: `{cfg['mean']}` | Std: `{cfg['std']}`\n\n"
                f"  📍 *{status}*\n"
                f"  ↳ Cách ngưỡng Long : `{spread - long_t:+.4f}`\n"
                f"  ↳ Cách ngưỡng Short: `{short_t - spread:+.4f}`\n"
            )
            cal_badge = "✅ Calibrated"
        else:
            threshold_line = f"  📋 *Chưa calibrate* – đang theo dõi để backtest\n"
            cal_badge = "🔬 Monitoring"

        lines.append(
            f"─────────────────────\n"
            f"*{cfg['label']}* `[{cal_badge}]`\n"
            f"  `{sym_a}`: `${price_a:.4f}` | `{sym_b}`: `${price_b:.4f}`\n"
            f"  Spread: `${spread:+.4f}`\n"
            f"{threshold_line}\n"
            f"  💸 *Funding* (`${vol:,}/leg`):\n"
            f"  Long {sym_a}+Short {sym_b}: {fmt_f(f_long_day, f_long_apr)}\n"
            f"  Short {sym_a}+Long {sym_b}: {fmt_f(f_short_day, f_short_apr)}\n"
        )

    lines.append("─────────────────────\n💡 /check · /symbols `<từ khóa>` · /help")
    return "\n".join(lines)


# ================================================================
# BUILD /symbols MESSAGE
# ================================================================
def build_symbols_message(keyword, prices, universe_names):
    kw = keyword.strip().upper()
    all_names = set(list(prices.keys()) + universe_names)
    matches = sorted([n for n in all_names if kw in n.upper()])

    if not matches:
        return (
            f"🔍 Không tìm thấy symbol nào chứa `{kw}`\n\n"
            f"Thử: `/symbols gold` · `/symbols xau` · `/symbols oil` · `/symbols cl`"
        )

    lines = [f"🔍 *Kết quả tìm `{kw}`* ({len(matches)} symbol):\n"]
    for name in matches[:20]:
        price = prices.get(name, "N/A")
        price_str = f"`${float(price):.4f}`" if price != "N/A" else "`(không có giá)`"
        lines.append(f"  • `{name}` → {price_str}")

    if len(matches) > 20:
        lines.append(f"\n_(còn {len(matches)-20} kết quả – dùng từ khóa cụ thể hơn)_")

    lines.append("\n👉 Copy tên chính xác vào `symbol_a` / `symbol_b` trong `CONFIG_PAIRS`")
    return "\n".join(lines)


# ================================================================
# TELEGRAM HELPER
# ================================================================
def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"⚠️ Telegram lỗi: {resp.status_code} – {resp.text}")
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")


# ================================================================
# /api – CRON JOB TRIGGER (chỉ scan cặp đã calibrated)
# ================================================================
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
            prices, funding_rates, _ = get_hyperliquid_data()

            for pair_key, cfg in CONFIG_PAIRS.items():

                # Bỏ qua cặp chưa calibrate
                if not cfg.get("calibrated"):
                    print(f"⏭ {pair_key}: chưa calibrate – bỏ qua scan")
                    continue

                sym_a, sym_b = cfg["symbol_a"], cfg["symbol_b"]
                price_a = float(prices.get(sym_a, 0))
                price_b = float(prices.get(sym_b, 0))

                print(f"--- {pair_key} | {sym_a}: {price_a} | {sym_b}: {price_b}")

                if price_a == 0 or price_b == 0:
                    print(f"❌ Không lấy được giá {sym_a} hoặc {sym_b}")
                    continue

                spread = price_a - price_b
                print(f"Spread: {spread:.4f}")

                # Xác định chiều trade
                is_long_a = None
                direction = ""
                if spread <= cfg["long_threshold"]:
                    is_long_a = True
                    direction = f"🟢 Long `{sym_a}` + Short `{sym_b}`"
                elif spread >= cfg["short_threshold"]:
                    is_long_a = False
                    direction = f"🔴 Short `{sym_a}` + Long `{sym_b}`"

                if is_long_a is None:
                    print("⏳ Spread bình thường.")
                    continue

                # Tính funding & PnL
                fa = funding_rates.get(sym_a, 0)
                fb = funding_rates.get(sym_b, 0)
                net_usd_day, net_apr_pct = calc_net_funding(fa, fb, is_long_a, cfg["vol_per_leg"])
                ev = evaluate_signal(cfg, spread, is_long_a, net_usd_day, price_a)

                print(f"Net PnL ước tính: ${ev['net_pnl']:.4f}")

                if ev["net_pnl"] <= 0:
                    print("🚫 Net PnL âm – bỏ qua.")
                    continue

                msg = build_signal_message(cfg, spread, direction, net_usd_day, net_apr_pct, ev)
                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                print(f"🚀 Đã gửi signal {pair_key}!")

            return jsonify({"status": "success"}), 200

        except Exception as e:
            print(f"Lỗi hệ thống: {e}")
            return str(e), 500

    return "Bot đang hoạt động. Dùng POST để trigger."


# ================================================================
# /webhook – NHẬN LỆNH TELEGRAM
# ================================================================
# Đăng ký 1 lần: https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<domain>/webhook
# ================================================================
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
    text_in = msg_obj.get("text", "").strip()

    ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        return "OK", 200

    cmd = text_in.lower()

    if cmd.startswith("/check"):
        try:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                  "⏳ Đang lấy dữ liệu từ Hyperliquid...")
            prices, funding_rates, universe_names = get_hyperliquid_data()
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                  build_check_message(prices, funding_rates))
        except Exception as e:
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: `{e}`")

    elif cmd.startswith("/symbols"):
        parts = text_in.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                "❓ Cú pháp: `/symbols <từ khóa>`\n"
                "Ví dụ: `/symbols gold` · `/symbols silver` · `/symbols oil`")
        else:
            try:
                kw = parts[1].strip()
                send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                      f"🔍 Đang tìm `{kw.upper()}` trên Hyperliquid...")
                prices, _, universe_names = get_hyperliquid_data()
                send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
                                      build_symbols_message(kw, prices, universe_names))
            except Exception as e:
                send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: `{e}`")

    elif cmd.startswith("/pairs"):
        # Liệt kê tất cả cặp đang cấu hình và trạng thái
        lines = ["📋 *Danh sách cặp đang cấu hình:*\n"]
        for key, cfg in CONFIG_PAIRS.items():
            badge = "✅ Active" if cfg.get("calibrated") else "🔬 Monitoring"
            lines.append(
                f"*{cfg['label']}* `[{badge}]`\n"
                f"  `{cfg['symbol_a']}` vs `{cfg['symbol_b']}`\n"
                f"  Vốn: `${cfg['vol_per_leg']:,}/leg`\n"
            )
        lines.append("💡 Dùng `/check` để xem giá thực tế")
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id, "\n".join(lines))

    elif cmd.startswith("/help"):
        send_telegram_message(TELEGRAM_BOT_TOKEN, chat_id,
            "🤖 *Bot Spread Trading – Lệnh khả dụng*\n\n"
            "/check – Snapshot tất cả cặp (giá, spread, funding)\n"
            "/pairs – Danh sách cặp đang theo dõi\n"
            "/symbols `<từ khóa>` – Tìm đúng tên symbol trên HL\n"
            "   ví dụ: `/symbols gold` · `/symbols silver` · `/symbols oil`\n"
            "/help – Menu này"
        )

    return "OK", 200