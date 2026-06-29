import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# CẤU HÌNH CÁC CẶP SPREAD
# ============================================================
# long_threshold  : spread <= giá trị này → Long A + Short B
# short_threshold : spread >= giá trị này → Short A + Long B
# max_adverse_funding_usd_day : ngưỡng funding bất lợi tối đa chấp nhận (USD/ngày)
#   - Nếu funding lợi (>= 0) → gửi signal ✅ đầy đủ
#   - Nếu funding bất lợi nhưng > max_adverse → gửi cảnh báo 🟡 kèm breakeven
#   - Nếu funding bất lợi và <= max_adverse → BỎ QUA, không gửi signal
# ============================================================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "WTI",
        "name_b": "Brent (B)",
        "symbol_b": "BRENT",
        "mean": -3.69,
        "std": 2.52,
        "long_threshold": -6.84,
        "short_threshold": -0.78,
        "vol_per_leg": 50_000,
        "max_adverse_funding_usd_day": -25.0,   # ví dụ: tối đa lỗ $25/ngày funding
    }
}

# ============================================================
# HÀM LẤY DỮ LIỆU TỪ HYPERLIQUID
# ============================================================
def get_hyperliquid_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}

    # Lấy mid-price tất cả asset
    prices = requests.post(
        url, headers=headers, json={"type": "allMids"}, timeout=10
    ).json()

    # Lấy funding rate
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
                # funding từ HL là rate per 1 giờ (dạng thập phân, e.g. 0.0001 = 0.01%)
                funding_dict[name] = float(asset_ctxs[i].get("funding", 0))

    return prices, funding_dict


# ============================================================
# TÍNH TOÁN FUNDING NET (có xét chiều trade)
# ============================================================
def calc_net_funding(funding_a, funding_b, is_long_a: bool, vol_per_leg: float):
    """
    Trả về (net_usd_per_day, net_apr_pct)

    Quy ước Hyperliquid:
      - funding > 0: Long phải TRẢ cho Short
      - funding < 0: Short phải TRẢ cho Long

    Khi Long A + Short B:
      - Với leg A (Long): nhận nếu funding_a < 0, trả nếu funding_a > 0
        → P&L funding A = -funding_a * vol_per_leg * 24
      - Với leg B (Short): nhận nếu funding_b > 0, trả nếu funding_b < 0
        → P&L funding B = +funding_b * vol_per_leg * 24
      → Net = (funding_b - funding_a) * vol_per_leg * 24

    Khi Short A + Long B → đảo dấu
    """
    if is_long_a:
        net_rate_daily = (funding_b - funding_a) * 24   # dạng thập phân/ngày
    else:
        net_rate_daily = (funding_a - funding_b) * 24

    net_usd_per_day = net_rate_daily * vol_per_leg
    # APR tính trên 1 leg vốn (vì hedge nên dùng 1 leg làm base)
    net_apr_pct = net_rate_daily * 365 * 100

    return net_usd_per_day, net_apr_pct


# ============================================================
# BUILD MESSAGE
# ============================================================
def build_message(config, current_spread, signal_direction, is_long_a,
                  net_usd_day, net_apr_pct, price_a):
    """
    Tạo message Telegram theo 3 trường hợp:
      - ✅ Funding có lợi
      - 🟡 Funding bất lợi nhưng chấp nhận được (kèm breakeven)
      - None → không gửi (funding quá bất lợi)
    """
    vol = config["vol_per_leg"]
    max_adverse = config["max_adverse_funding_usd_day"]
    name_a, name_b = config["name_a"], config["name_b"]
    long_t = config["long_threshold"]
    short_t = config["short_threshold"]

    # ── Xác định trạng thái funding ──
    if net_usd_day >= 0:
        funding_status = "favorable"
    elif net_usd_day >= max_adverse:
        funding_status = "acceptable"
    else:
        return None  # funding quá bất lợi → không gửi

    # ── Header chung ──
    header = (
        f"🚨 *Signal Adaptive mới!*\n\n"
        f"🥇 *{name_a} vs {name_b}*\n"
        f"Spread: `${current_spread:+.2f}` (L<{long_t} / S>{short_t})\n"
        f"➔ {signal_direction}\n"
    )

    sign = "+" if net_usd_day >= 0 else ""

    if funding_status == "favorable":
        # Tính số đơn vị (barrels / oz) xấp xỉ
        units = vol / price_a if price_a > 0 else 0
        body = (
            f"✅ Funding `{sign}${net_usd_day:.1f}/ngày` "
            f"(net APR `{net_apr_pct:+.1f}%` · `${vol:,}/leg`)\n"
            f"  Ước tính funding (`${vol:,}/leg` · `~{units:.2f} units`)\n"
            f"💰 Lợi nhuận FR: `{sign}${net_usd_day:.2f}/ngày` (net APR `{net_apr_pct:.2f}%`)"
        )
    else:
        # Tính breakeven: spread cần dịch chuyển bao nhiêu $/ngày để bù funding
        units = vol / price_a if price_a > 0 else 1
        breakeven_spread_per_day = abs(net_usd_day) / units if units > 0 else 0
        body = (
            f"🟡 Funding `{sign}${net_usd_day:.1f}/ngày` — chấp nhận được "
            f"(≥`${max_adverse}/ngày` · net APR `{net_apr_pct:+.1f}%`)\n"
            f"  Ước tính funding (`${vol:,}/leg` · `~{units:.2f} units`)\n"
            f"💸 Chi phí FR: `${net_usd_day:.2f}/ngày` (net APR `{net_apr_pct:.2f}%`)\n"
            f"⚖️ Hòa vốn funding: spread cần tăng `${breakeven_spread_per_day:.2f}/ngày`\n"
            f"   (tương đương `${abs(net_usd_day):.0f}` PnL spread/ngày trên position)"
        )

    footer = (
        f"\n\n📊 Mean: `{config['mean']}` | Std: `{config['std']}`"
    )

    return header + body + footer


# ============================================================
# TELEGRAM HELPER
# ============================================================
def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"⚠️ Telegram API lỗi: {resp.status_code} – {resp.text}")
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")


# ============================================================
# API ENDPOINT
# ============================================================
@app.route("/api", methods=["GET", "POST"])
def scan_bot():
    if request.method == "POST":
        # Xác thực secret từ cron-job
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
                sym_a = config["symbol_a"]
                sym_b = config["symbol_b"]

                price_a = float(prices.get(sym_a, 0))
                price_b = float(prices.get(sym_b, 0))

                print(f"--- Quét cặp {pair_key} ---")
                print(f"Giá {sym_a}: {price_a} | Giá {sym_b}: {price_b}")

                if price_a == 0 or price_b == 0:
                    print(f"❌ Không tìm thấy giá {sym_a} hoặc {sym_b}. "
                          f"Kiểm tra lại ký hiệu!")
                    continue

                current_spread = price_a - price_b
                print(f"Spread hiện tại: {current_spread:.4f}")

                # ── Xác định chiều trade ──
                is_long_a = None
                signal_direction = ""

                if current_spread <= config["long_threshold"]:
                    is_long_a = True
                    signal_direction = (
                        f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                    )
                elif current_spread >= config["short_threshold"]:
                    is_long_a = False
                    signal_direction = (
                        f"🔴 Short {config['name_a']} + Long {config['name_b']}"
                    )

                if is_long_a is None:
                    print("⏳ Spread bình thường, chưa đạt ngưỡng.")
                    continue

                # ── Tính funding đúng chiều ──
                funding_a = funding_rates.get(sym_a, 0)
                funding_b = funding_rates.get(sym_b, 0)

                net_usd_day, net_apr_pct = calc_net_funding(
                    funding_a, funding_b, is_long_a, config["vol_per_leg"]
                )

                print(f"Funding net: ${net_usd_day:.2f}/ngày | APR: {net_apr_pct:.2f}%")

                # ── Build & gửi message ──
                msg = build_message(
                    config, current_spread, signal_direction,
                    is_long_a, net_usd_day, net_apr_pct, price_a
                )

                if msg is None:
                    print(
                        f"🚫 Funding quá bất lợi (${net_usd_day:.1f}/ngày < "
                        f"${config['max_adverse_funding_usd_day']}/ngày). Bỏ qua signal."
                    )
                    continue

                send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                print("🚀 Đã gửi signal về Telegram!")

            return jsonify({"status": "success"}), 200

        except Exception as e:
            print(f"Lỗi hệ thống: {e}")
            return str(e), 500

    return "Bot đang hoạt động (Flask). Dùng POST để trigger."