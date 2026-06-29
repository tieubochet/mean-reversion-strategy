from http.server import BaseHTTPRequestHandler
import json
import os
import requests

# --- CẤU HÌNH THAM SỐ TĨNH (Giữ nguyên từ kết quả Backtest) ---
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "CL",
        "name_b": "Brent (B)",
        "symbol_b": "BRENTOIL",
        "mean": -3.69,
        "std": 2.52,
        "long_threshold": -6.84,
        "short_threshold": -0.78,
        "vol_per_leg": 50000
    }
}

def get_hyperliquid_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    payload_prices = {"type": "allMids"}
    response_prices = requests.post(url, headers=headers, json=payload_prices).json()
    
    payload_funding = {"type": "metaAndAssetCtxs"}
    response_funding = requests.post(url, headers=headers, json=payload_funding).json()
    
    funding_dict = {}
    if isinstance(response_funding, list) and len(response_funding) > 1:
        universe = response_funding[0].get("universe", [])
        asset_ctxs = response_funding[1]
        for i, asset in enumerate(universe):
            name = asset.get("name")
            if i < len(asset_ctxs):
                funding_rate = float(asset_ctxs[i].get("funding", 0))
                funding_dict[name] = funding_rate

    return response_prices, funding_dict

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

# --- SERVERLESS FUNCTION HANDLER ---
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. Bảo mật: Kiểm tra mã Secret Key từ cron-job.org gửi sang (Tránh người lạ tự ý bấm vào link API của bạn)
        CRON_SECRET = os.environ.get("CRON_SECRET")
        incoming_secret = self.headers.get("X-Cron-Secret")
        
        if CRON_SECRET and incoming_secret != CRON_SECRET:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized - Sai Secret Key")
            return

        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Thieu bien moi truong TELEGRAM")
            return

        try:
            prices, funding_rates = get_hyperliquid_data()
            
            for pair_key, config in CONFIG_PAIRS.items():
                price_a = float(prices.get(config["symbol_a"], 0))
                price_b = float(prices.get(config["symbol_b"], 0))
                
                if price_a == 0 or price_b == 0:
                    continue
                
                current_spread = price_a - price_b
                is_triggered = False
                signal_direction = ""
                
                if current_spread <= config["long_threshold"]:
                    is_triggered = True
                    signal_direction = f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                elif current_spread >= config["short_threshold"]:
                    is_triggered = True
                    signal_direction = f"🔴 Short {config['name_a']} + Long {config['name_b']}"
                
                if is_triggered:
                    funding_a = funding_rates.get(config["symbol_a"], 0)
                    funding_b = funding_rates.get(config["symbol_b"], 0)
                    
                    net_funding_rate_daily = (funding_a - funding_b) * 24 * 100
                    est_funding_amount = (config["vol_per_leg"] * net_funding_rate_daily) / 100
                    
                    msg = (
                        f"🚨 *Signal Adaptive mới!*\n\n"
                        f"🥇 *{config['name_a']} vs {config['name_b']}*\n"
                        f"Spread: ${current_spread:.2f} (L<{config['long_threshold']} / S>{config['short_threshold']})\n"
                        f"➔ {signal_direction}\n"
                        f"✅ Funding ước tính: {'+' if est_funding_amount >= 0 else ''}${est_funding_amount:.1f}/ngày\n"
                        f"Mức Mean lịch sử: {config['mean']} | Std: {config['std']}"
                    )
                    
                    send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode())
            
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())