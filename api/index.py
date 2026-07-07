import os
import requests
from flask import Flask, request
from datetime import datetime, timezone
import pandas as pd
import numpy as np

app = Flask(__name__)

# ==================== CONFIG ====================
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "XYZ:CL",
        "name_b": "Brent (B)",
        "symbol_b": "XYZ:BRENTOIL",

        # Tham số backtest M15 (giữ nguyên)
        "mean": -3.80,
        "std": 1.72,

        "use_zscore": True,
        "long_z_threshold": -1.6,
        "short_z_threshold": 1.6,
        "exit_z_threshold": 0.30,

        "vol_per_leg": 14000,
        "avg_hold_hours": 16.5,
        "fee_bps": 0.00022,
        "min_net_pnl": 35,
        "max_funding_loss": 45,
        
        # Thêm: Số nến dùng để tính rolling Z-score (30 ngày M15)
        "rolling_window_candles": 30 * 4 * 24
    }
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ==================== HÀM HỖ TRỢ ====================
def calculate_z_score(spread, mean, std):
    return 0 if std == 0 else (spread - mean) / std

def get_hyperliquid_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    prices, funding_dict = {}, {}

    try:
        mids_resp = requests.post(url, headers=headers, json={"type": "allMids", "dex": "xyz"}, timeout=8).json()
        if isinstance(mids_resp, dict):
            prices = {k.upper(): float(v) for k, v in mids_resp.items() if v}
    except Exception as e:
        print(f"Lỗi lấy allMids: {e}")

    try:
        meta_resp = requests.post(url, headers=headers, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=8).json()
        if isinstance(meta_resp, list) and len(meta_resp) >= 2:
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]
            for i, asset in enumerate(universe):
                coin = asset.get("name", "").upper()
                if i < len(asset_ctxs):
                    funding = float(asset_ctxs[i].get("funding", 0))
                    funding_dict[coin] = funding
                    if not coin.startswith("XYZ:"):
                        funding_dict[f"XYZ:{coin}"] = funding
    except Exception as e:
        print(f"Lỗi lấy funding: {e}")

    return prices, funding_dict

def calc_net_funding(funding_a, funding_b, is_long_a, vol):
    net_rate = (funding_b - funding_a) if is_long_a else (funding_a - funding_b)
    daily = net_rate * 24 * vol
    apr = net_rate * 24 * 365 * 100
    return daily, apr

def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        print(f"[TG] {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=8)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

# ==================== XỬ LÝ CHÍNH ====================
def process_pair(pair_key, cfg, prices, funding_rates):
    sym_a = cfg["symbol_a"].upper()
    sym_b = cfg["symbol_b"].upper()

    price_a = float(prices.get(sym_a, prices.get(sym_a.replace("XYZ:", ""), 0)))
    price_b = float(prices.get(sym_b, prices.get(sym_b.replace("XYZ:", ""), 0)))

    if price_a == 0 or price_b == 0:
        return

    spread = price_a - price_b
    z_score = calculate_z_score(spread, cfg["mean"], cfg["std"])

    fa = funding_rates.get(sym_a, funding_rates.get(sym_a.replace("XYZ:", ""), 0))
    fb = funding_rates.get(sym_b, funding_rates.get(sym_b.replace("XYZ:", ""), 0))

    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

    # Xác định hướng
    if z_score <= cfg["long_z_threshold"]:
        side = "LONG"
        direction = f"🟢 *LONG SPREAD*: BUY {cfg['name_a']} & SELL {cfg['name_b']}"
        net_usd, net_apr = calc_net_funding(fa, fb, True, cfg["vol_per_leg"])
    elif z_score >= cfg["short_z_threshold"]:
        side = "SHORT"
        direction = f"🔴 *SHORT SPREAD*: SELL {cfg['name_a']} & BUY {cfg['name_b']}"
        net_usd, net_apr = calc_net_funding(fa, fb, False, cfg["vol_per_leg"])
    else:
        return  # Chưa đạt ngưỡng Z

    # Tính expected PnL
    expected_pnl = net_usd * (cfg["avg_hold_hours"] / 24)
    min_pnl = cfg.get("min_net_pnl", 0)
    max_loss = cfg.get("max_funding_loss", 999)

    funding_ok = (net_usd >= 0) or (abs(net_usd) <= max_loss)
    pnl_ok = expected_pnl >= min_pnl

    # ==================== GỬI TELEGRAM ====================
    if funding_ok and pnl_ok:
        # Trường hợp ĐẠT
        msg = (
            f"🚨 *TÍN HIỆU MEAN-REVERSION (M15)*\n"
            f"🕐 `{now}`\n\n"
            f"🛢 *{cfg['name_a']} vs {cfg['name_b']}*\n"
            f"Spread: `${spread:+.2f}`\n"
            f"Z-Score: `{z_score:+.2f}` (Fixed) | Rolling: `N/A`\n\n"
            f"{direction}\n\n"
            f"💰 *Funding & PnL (Vốn ${cfg['vol_per_leg']:,})*\n"
            f"• Net Funding: `{net_usd:+.2f}$/ngày`\n"
            f"• Expected PnL ({cfg['avg_hold_hours']}h): `{expected_pnl:+.2f}$`\n"
            f"• APR: `{net_apr:+.1f}%`\n\n"
            f"✅ Đạt cả Spread + Funding/PnL filter"
        )
        send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)

    else:
        # Trường hợp BỊ LỌC → VẪN GỬI TELEGRAM
        reason = []
        if not funding_ok:
            reason.append(f"Funding quá tiêu cực ({net_usd:+.2f}$/ngày > {max_loss}$)")
        if not pnl_ok:
            reason.append(f"Expected PnL thấp ({expected_pnl:+.2f}$ < {min_pnl}$)")

        msg = (
            f"⚠️ *BỎ QUA TÍN HIỆU (M15)*\n"
            f"🕐 `{now}`\n\n"
            f"🛢 *{cfg['name_a']} vs {cfg['name_b']}*\n"
            f"Spread: `${spread:+.2f}` | Z-Score: `{z_score:+.2f}`\n"
            f"Hướng: {side}\n\n"
            f"❌ *Lý do bỏ qua:*\n• " + "\n• ".join(reason) + "\n\n"
            f"💡 Funding: `{net_usd:+.2f}$/ngày` | Expected PnL: `{expected_pnl:+.2f}$`"
        )
        send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)

# ==================== ENDPOINTS ====================
@app.route("/api", methods=["POST", "GET"])
def scan_bot():
    try:
        prices, funding_rates = get_hyperliquid_data()
        for pair_key, cfg in CONFIG_PAIRS.items():
            process_pair(pair_key, cfg, prices, funding_rates)
        return {"status": "success"}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route("/api/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True)
    if not update: return "OK", 200
    msg = update.get("message") or update.get("edited_message")
    if not msg: return "OK", 200

    text = msg.get("text", "").strip().lower()
    chat_id = str(msg["chat"]["id"])

    if text.startswith("/check"):
        try:
            prices, funding = get_hyperliquid_data()
            # Gửi snapshot nhanh
            send_telegram(TELEGRAM_BOT_TOKEN, chat_id, "⏳ Đang lấy dữ liệu...")
            # Có thể mở rộng build_check_message nếu cần
        except Exception as e:
            send_telegram(TELEGRAM_BOT_TOKEN, chat_id, f"❌ Lỗi: {str(e)}")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)