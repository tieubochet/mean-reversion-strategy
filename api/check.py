"""
Endpoint: GET/POST /api/check
Quét tín hiệu hiện tại theo yêu cầu thủ công. LUÔN gửi 1 tin Telegram báo
trạng thái. Logic nằm ở _pairs_bot.py (cùng thư mục api/).
"""

import json
from http.server import BaseHTTPRequestHandler
from _pairs_bot import (
    evaluate_signal,
    send_telegram_message,
    build_check_message,
    result_to_json,
    check_auth,
)


class handler(BaseHTTPRequestHandler):
    def _handle(self):
        if not check_auth(self.headers):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            result = evaluate_signal(force_funding_check=True)
            send_telegram_message(build_check_message(result))
            response = result_to_json(result)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response, indent=2).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()