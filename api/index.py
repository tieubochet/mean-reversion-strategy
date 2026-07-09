"""
Endpoint: POST/GET /api/index
Dùng cho cron-job.org — ping mỗi 5 phút, quét tín hiệu và GỬI TELEGRAM khi
đủ điều kiện (|z| >= SIGNAL_THRESHOLD và net kỳ vọng > 0 sau khi trừ phí +
funding cost). Toàn bộ logic nằm ở lib/pairs_bot.py.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.pairs_bot import (
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
        # cron-job.org ping bằng POST mỗi 5 phút -> đường chính
        self._handle()

    def do_GET(self):
        # giữ lại để test thủ công bằng curl/trình duyệt
        self._handle()