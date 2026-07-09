"""
Endpoint: GET/POST /api/check
Quét tín hiệu hiện tại BẤT CỨ LÚC NÀO theo yêu cầu thủ công (mở trình duyệt,
curl, Postman...). Mỗi lần gọi LUÔN gửi 1 tin Telegram báo trạng thái hiện
tại (dù đang có tín hiệu vào lệnh hay không) — khác với /api/index chỉ gửi
Telegram khi thật sự đủ điều kiện vào lệnh.

/check LUÔN tính cả funding cost (kể cả khi |z| chưa vượt ngưỡng) để bạn xem
trước funding hiện tại đang tốn/lời bao nhiêu.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.pairs_bot import (
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
            # force_funding_check=True -> luôn tính đủ funding để báo cáo đầy đủ
            result = evaluate_signal(force_funding_check=True)

            # /check LUÔN gửi Telegram, khác /index chỉ gửi khi should_enter=True
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
