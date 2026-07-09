# Pairs Trading Signal Bot — WTI (xyz:CL) vs Brent (xyz:BRENTOIL)

Bot theo dõi z-score của spread WTI-Brent trên Hyperliquid, kiểm tra funding
rate trước khi báo tín hiệu, và gửi vào Telegram khi đủ điều kiện.

## Cấu trúc project

```
telegram-pairs-bot/
├── lib/
│   ├── __init__.py
│   └── pairs_bot.py     # TOÀN BỘ logic dùng chung: config, fetch data,
│                         # tính z-score, tính funding cost, gửi Telegram
├── api/
│   ├── index.py          # POST /api/index — cron-job.org ping mỗi 5 phút,
│                          #   quét tín hiệu + GỬI Telegram nếu đủ điều kiện
│   └── check.py           # GET/POST /api/check — quét thủ công bất cứ lúc
│                           #   nào, CHỈ trả JSON, KHÔNG gửi Telegram
├── requirements.txt
└── README.md
```

Vì mỗi file trong `api/` là 1 route riêng trên Vercel, không thể gộp 2
endpoint vào 1 file — nên toàn bộ logic dùng chung được gom vào
`lib/pairs_bot.py`, còn `api/index.py` và `api/check.py` chỉ là 2 wrapper
mỏng gọi vào đó (không trùng lặp code).

## 2 endpoint — 2 trường hợp gửi Telegram

| Endpoint | Method | Mục đích | Gửi Telegram? |
|---|---|---|---|
| `/api/index` | POST (cron-job.org dùng), GET (test tay) | Quét tín hiệu định kỳ mỗi 5 phút | **Có** — chỉ khi `\|z\| >= SIGNAL_THRESHOLD` và net kỳ vọng > 0 (tin "PAIRS SIGNAL") |
| `/api/check` | GET hoặc POST | Quét tín hiệu hiện tại theo yêu cầu, bất cứ lúc nào | **Có** — luôn gửi 1 tin "CHECK PAIRS STATUS" mỗi lần gọi, dù có tín hiệu vào lệnh hay không |

`/check` luôn tính cả funding cost dù `|z|` chưa vượt ngưỡng, để bạn xem
trước funding hiện tại đang "ăn" bao nhiêu vào lợi nhuận kỳ vọng ngay trong
tin nhắn Telegram — hữu ích để chủ động theo dõi thị trường bất cứ lúc nào
mà không cần chờ tín hiệu tự động.

⚠️ Vì `/check` luôn gửi Telegram, **đừng gắn cron vào `/check`** (sẽ spam
liên tục) — chỉ gọi thủ công khi bạn cần xem trạng thái.

## Logic báo tín hiệu (trong `lib/pairs_bot.py`, dùng chung cho cả 2 endpoint)

1. Lấy giá hiện tại của 2 leg → tính spread hiện tại.
2. So spread với `SPREAD_MEAN` / `SPREAD_STD` **cố định** (không tính lại
   rolling mỗi lần chạy) → ra z-score.
3. Nếu `|z| < SIGNAL_THRESHOLD` → dừng (trên `/index`), hoặc vẫn tính funding
   tham khảo (trên `/check`).
4. Nếu `|z| >= SIGNAL_THRESHOLD` → gọi thêm API funding rate của cả 2 leg,
   quy đổi ra chi phí funding **mỗi ngày** cho vị thế dự kiến, nhân với số
   ngày hold kỳ vọng (`EXPECTED_HOLD_DAYS`).
5. So sánh:
   ```
   Net kỳ vọng = Lợi nhuận kỳ vọng (spread hồi mean) − Phí trade − Chi phí funding kỳ vọng
   ```
   Chỉ `/api/index` gửi tín hiệu Telegram khi Net kỳ vọng > 0.

## Thông số đã chốt từ backtest (Task 2 & 3)

| Env var | Giá trị mặc định | Nguồn |
|---|---|---|
| `SPREAD_MEAN` | -3.2858 | Task 2, 52 ngày data |
| `SPREAD_STD` | 0.4675 | Task 2, 52 ngày data |
| `SIGNAL_THRESHOLD` | 1.5 | Task 3, winrate 89.3%, 121 trades |
| `EXIT_Z_THRESHOLD` | 0.0 | khớp quy tắc exit dùng trong backtest (thoát khi spread về đúng Mean) |
| `EXPECTED_HOLD_DAYS` | 0.264 (~379.7 phút) | Task 3, hold time trung bình threshold=1.5 |
| `CAPITAL_PER_LEG` | 5000 | đề bài |
| `FEE_BPS_PER_FILL` | 2.2 | đề bài |
| `FILLS_PER_ROUND` | 4 | đề bài |

Tất cả override được qua Environment Variables trên Vercel, không cần sửa
code hay redeploy. Cả `/api/index` và `/api/check` đều đọc chung 1 bộ env var.

## Recalibrate hàng tuần

1. Chạy lại `fetch_spread_stats.py` trên dữ liệu 90 ngày mới nhất.
2. Lấy Mean/Std mới từ `spread_stats_table.md`.
3. Vào Vercel → Project Settings → Environment Variables → update
   `SPREAD_MEAN`, `SPREAD_STD`.
4. (Tuỳ chọn) chạy lại `backtest_pairs.py` để kiểm tra `SIGNAL_THRESHOLD` và
   `EXPECTED_HOLD_DAYS` còn hợp lý không, update nếu cần.
5. Không cần redeploy — Vercel áp dụng env var mới ngay cho lần gọi kế tiếp.

## Setup từng bước

### 1. Tạo Telegram bot
- Chat với [@BotFather](https://t.me/BotFather) → `/newbot` → lấy `TELEGRAM_BOT_TOKEN`.
- Lấy `TELEGRAM_CHAT_ID`: nhắn thử 1 tin cho bot, sau đó mở
  `https://api.telegram.org/bot<TOKEN>/getUpdates` để xem `chat.id`.

### 2. Push code lên GitHub
```bash
git init
git add .
git commit -m "init pairs trading signal bot"
git remote add origin https://github.com/<your-username>/<repo>.git
git push -u origin main
```

### 3. Deploy lên Vercel
- [vercel.com](https://vercel.com) → New Project → import repo GitHub vừa tạo.
- Vercel tự nhận `api/index.py` và `api/check.py` là 2 Python serverless
  function riêng biệt (đều import chung `lib/pairs_bot.py`).
- Vào **Project Settings → Environment Variables**, thêm:

  | Key | Value |
  |---|---|
  | `TELEGRAM_BOT_TOKEN` | token từ BotFather |
  | `TELEGRAM_CHAT_ID` | chat id của bạn |
  | `CRON_SECRET` | chuỗi ngẫu nhiên ≥16 ký tự tự đặt |
  | `SPREAD_MEAN` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SPREAD_STD` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SIGNAL_THRESHOLD` | để trống nếu dùng mặc định 1.5 |
  | `EXPECTED_HOLD_DAYS` | để trống nếu dùng mặc định |

- Deploy. Copy URL project, VD: `https://your-project.vercel.app`.

### 4. Test thủ công trước khi gắn cron
```bash
# Test /check - LUÔN gửi 1 tin Telegram báo trạng thái hiện tại
curl -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api/check

# Test /index - giống logic cron thật, chỉ gửi Telegram nếu đủ điều kiện vào lệnh
curl -X POST -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api/index
```
Sau khi gọi `/check`, kiểm tra Telegram — phải thấy 1 tin "[CHECK] PAIRS
STATUS" xuất hiện ngay, dù `should_enter` là `true` hay `false`.

Kỳ vọng JSON trả về, VD:
```json
{"z": -0.42, "spread": -3.48, "should_enter": false, "reason": "z-score dưới ngưỡng (đã tính funding tham khảo)", "expected_pnl": 0.5, "net_expected": -4.1}
```
Nếu lỗi 401 → sai `CRON_SECRET`; lỗi 500 → xem message trong JSON (thường do
thiếu env var hoặc Hyperliquid API đổi format).

### 5. Cấu hình cron-job.org (chỉ trỏ vào /api/index)
- Đăng ký/đăng nhập [cron-job.org](https://cron-job.org).
- **Create cronjob**:
  - **URL**: `https://your-project.vercel.app/api/index`
  - **Schedule**: Every 5 minutes (`*/5 * * * *`)
  - **Request method**: **POST**
  - **Headers**: `Authorization: Bearer <CRON_SECRET>` (đúng giá trị đã set trên Vercel)
  - **Notifications**: nên bật "notify on failure".
  - Save & Enable.

Không gắn cron vào `/api/check` — endpoint đó gửi Telegram mỗi lần gọi, dành
cho bạn tự gọi thủ công khi muốn xem trạng thái. Gắn cron vào nó sẽ spam
Telegram mỗi 5 phút dù có tín hiệu hay không.

## Giới hạn cần biết

- **Bot CHƯA có tín hiệu đóng lệnh tự động.** Cả `/api/index` lẫn `/api/check`
  chỉ đánh giá "có nên vào lệnh không" mỗi lần chạy, không biết bạn đang có
  vị thế mở hay không. Tin nhắn tín hiệu (và `/check`) có kèm dòng **"🎯 Gợi ý
  đóng lệnh"** — mức spread ứng với `EXIT_Z_THRESHOLD` (mặc định z=0, tức khi
  spread hồi đúng về Mean, giống quy tắc exit trong backtest Task 3) — nhưng
  đây chỉ là **con số tham khảo tại thời điểm vào lệnh**, không phải cảnh báo
  tự động khi giá thực sự chạm mức đó. Bạn cần tự theo dõi bằng cách gọi lại
  `/check` định kỳ, hoặc đặt sẵn take-profit/limit order trên sàn ngay khi
  vào lệnh.
  Muốn có thông báo tự động **khi thực sự tới điểm đóng** (không chỉ gợi ý
  lúc vào lệnh), cần thêm state lưu vị thế đang mở (Vercel KV/Upstash Redis)
  — báo tôi nếu bạn muốn triển khai tiếp phần này.
- **`/api/index` vẫn stateless** cho phần vào lệnh — mỗi lần cron chạy tự đánh giá lại từ đầu,
  không nhớ "đã có vị thế đang mở" hay chưa. Nếu điều kiện vào lệnh tiếp tục
  đúng trong nhiều chu kỳ 5 phút liên tiếp, bạn sẽ nhận tín hiệu lặp lại
  nhiều lần. Muốn tránh spam, cần thêm state (Vercel KV hoặc Upstash Redis)
  để chỉ bắn khi **chuyển trạng thái** (flat → in-signal). Báo tôi nếu muốn
  bổ sung phần này.
- Funding rate lấy tại **thời điểm hiện tại**, không phải trung bình dự kiến
  suốt thời gian hold — chi phí thực tế có thể khác số ước tính lúc vào lệnh.
- Đây là **bot báo tín hiệu**, không tự đặt lệnh.
- Backtest Task 3 chỉ dựa trên **52 ngày dữ liệu** (chưa đủ 90 ngày) và
  **không tính funding rate lịch sử lẫn slippage** — kết quả live thực tế
  nhiều khả năng khác so với số backtest. Nên theo dõi sát 2-4 tuần đầu
  trước khi tăng size.