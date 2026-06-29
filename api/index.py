import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/api', methods=['GET', 'POST'])
def test_bot():
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    
    # Thử gửi một tin nhắn test thuần túy không có ký tự đặc biệt
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "PING: Bot cua ban da ket noi thanh cong!"}
    
    res = requests.post(url, json=payload).json()
    
    # Trả kết quả của Telegram về màn hình để xem lỗi
    return jsonify({
        "status": "Executed",
        "telegram_response": res
    }), 200