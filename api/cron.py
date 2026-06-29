from http.server import BaseHTTPRequestHandler
import json
import os
import requests

# --- CẤU HÌNH THAM SỐ TĨNH (Lấy từ kết quả Backtest của bạn) ---
# Bạn có thể thêm nhiều cặp vào đây tùy ý
CONFIG_PAIRS = {
    "WTI_BRENT": {
        "name_a": "WTI (A)",
        "symbol_a": "CL",        # Ký hiệu trên Hyperliquid (thường là tên tài sản)
        "name_b": "Brent (B)",
        "symbol_b": "BRENTOIL",
        "mean": -3.69,
        "std": 2.52,
        "long_threshold": -6.84,  # Dùng mốc P10 như bạn phân tích
        "short_threshold": -0.78, # Dùng mốc P90
        "vol_per_leg": 50000      # Vốn giả định $50,000/leg
    }
}

# --- CÁC HÀM TRỢ GIÚP (HELPERS) ---

def get_hyperliquid_data():
    """Gọi API Hyperliquid lấy giá mid và funding rate dự kiến"""
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    # Lấy giá hiện tại (allMids)
    payload_prices = {"type": "allMids"}
    response_prices = requests.post(url, headers=headers, json=payload_prices).json()
    
    # Lấy thông tin Funding (metaAndAssetCtxs)
    payload_funding = {"type": "metaAndAssetCtxs"}
    response_funding = requests.post(url, headers=headers, json=payload_funding).json()
    
    # Bóc tách dữ liệu funding của các cặp perp
    funding_dict = {}
    if isinstance(response_funding, list) and len(response_funding) > 1:
        universe = response_funding[0].get("universe", [])
        asset_ctxs = response_funding[1]
        for i, asset in enumerate(universe):
            name = asset.get("name")
            if i < len(asset_ctxs):
                # Funding rate mỗi chu kỳ (Hyperliquid tính theo giờ hoặc tùy cơ chế, đổi ra ngày)
                funding_rate = float(asset_ctxs[i].get("funding", 0))
                funding_dict[name] = funding_rate

    return response_prices, funding_dict

def send_telegram_message(token, chat_id, text):
    """Gửi tin nhắn thông báo qua Telegram Bot API"""
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

# --- PHẦN XỬ LÝ CHÍNH KHI VERCEL GỌI CRON ---

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. Lấy biến môi trường bảo mật từ Vercel Env
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Thieu bien moi truong TELEGRAM")
            return

        try:
            # 2. Lấy dữ liệu Realtime từ sàn
            prices, funding_rates = get_hyperliquid_data()
            
            # 3. Quét qua từng cặp cấu hình
            for pair_key, config in CONFIG_PAIRS.items():
                price_a = float(prices.get(config["symbol_a"], 0))
                price_b = float(prices.get(config["symbol_b"], 0))
                
                if price_a == 0 or price_b == 0:
                    continue  # Bỏ qua nếu không lấy được giá
                
                # Tính Spread hiện tại
                current_spread = price_a - price_b
                
                # Kiểm tra điều kiện kích hoạt chiến lược
                is_triggered = False
                signal_direction = ""
                
                if current_spread <= config["long_threshold"]:
                    is_triggered = True
                    signal_direction = f"🟢 Long {config['name_a']} + Short {config['name_b']}"
                elif current_spread >= config["short_threshold"]:
                    is_triggered = True
                    signal_direction = f"🔴 Short {config['name_a']} + Long {config['name_b']}"
                
                # 4. Nếu thỏa mãn điều kiện Spread -> Check tiếp Funding
                if is_triggered:
                    funding_a = funding_rates.get(config["symbol_a"], 0)
                    funding_b = funding_rates.get(config["symbol_b"], 0)
                    
                    # Giả định tính toán funding nhận được/phải trả đơn giản 
                    # Lợi nhuận FR = (Funding nhận - Funding trả) * Vol
                    # Lưu ý: Tùy thuộc vào hướng đi Long/Short mà dấu (+/-) của FR sẽ thay đổi lợi nhuận.
                    # Dưới đây là công thức mang tính minh họa dựa trên logic tính toán toán học:
                    net_funding_rate_daily = (funding_a - funding_b) * 24 * 100 # quy đổi ra % ngày (ước tính)
                    est_funding_amount = (config["vol_per_leg"] * net_funding_rate_daily) / 100
                    
                    # Định dạng tin nhắn gửi đi giống như mẫu của bạn
                    msg = (
                        f"🚨 *Signal Adaptive mới!*\n\n"
                        f"🥇 *{config['name_a']} vs {config['name_b']}*\n"
                        f"Spread: ${current_spread:.2f} (L<{config['long_threshold']} / S>{config['short_threshold']})\n"
                        f"➔ {signal_direction}\n"
                        f"✅ Funding ước tính: {'+' if est_funding_amount >= 0 else ''}${est_funding_amount:.1f}/ngày\n"
                        f"Mức Mean lịch sử: {config['mean']} | Std: {config['std']}"
                    )
                    
                    # Gửi alert
                    send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)

            # Phản hồi thành công cho Vercel Cron
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Cron executed successfully"}).encode())
            
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())