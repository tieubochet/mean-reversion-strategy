# --- SERVERLESS FUNCTION HANDLER (Hỗ trợ phương thức POST + Request Body) ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 1. Đọc dữ liệu JSON gửi từ Request Body của cron-job.org
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        incoming_secret = None
        try:
            body_data = json.loads(post_data.decode('utf-8'))
            incoming_secret = body_data.get("secret") # Lấy trường "secret" từ JSON
        except Exception:
            pass

        # 2. Kiểm tra tính bảo mật
        CRON_SECRET = os.environ.get("CRON_SECRET")
        if CRON_SECRET and incoming_secret != CRON_SECRET:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized - Sai Secret Key")
            return

        # 3. Tiến hành logic quét dữ liệu (Giữ nguyên phần code xử lý phía dưới)
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

    # Vẫn giữ lại do_GET để bạn dễ dàng test nhanh trên trình duyệt nếu cần
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot dang hoat dong dung huong. Hay dung phương thức POST de trigger.")