"""
Endpoint: POST/GET /api/index
Dùng cho cron-job.org — ping mỗi 5 phút, quét tín hiệu và GỬI TELEGRAM khi
đủ điều kiện. Toàn bộ logic nằm ở _pairs_bot.py (cùng thư mục api/, tên bắt
đầu bằng "_" nên Vercel không coi nó là 1 route riêng).
"""

import json
from http.server import BaseHTTPRequestHandler
from _pairs_bot import (
    evaluate_signal,
    send_telegram_message,
    build_signal_message,
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
            result = evaluate_signal()

            if result["should_enter"]:
                send_telegram_message(build_signal_message(result))

            response = result_to_json(result)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()