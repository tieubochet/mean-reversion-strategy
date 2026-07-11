# Pairs Trading Signal Bot — WTI (xyz:CL) vs Brent (xyz:BRENTOIL)

Bot theo dõi z-score của spread WTI-Brent trên Hyperliquid, kiểm tra funding
rate trước khi báo tín hiệu, và gửi vào Telegram khi đủ điều kiện. Hỗ trợ cả
quét tự động theo lịch (cron) lẫn quét theo yêu cầu ngay trong chat Telegram
(gõ `/check`).

## Cấu trúc project

```
telegram-pairs-bot/
├── api/
│   └── index.py    # 1 FILE Flask app duy nhất — toàn bộ logic VÀ 2 route
│                     # nội bộ: /api (cron) và /api/webhook (Telegram).
├── vercel.json      # "rewrites" trỏ /api và /api/webhook về /api/index
│                      # (BẮT BUỘC — không có sẽ ra 404, xem giải thích bên
│                      # dưới) + maxDuration=30s.
├── requirements.txt  # requests, flask
└── README.md
```

Chỉ 1 file Python — Flask tự định tuyến `/api` và `/api/webhook` dựa vào
`request.path` **thật** của request, không có code nào tự đoán "request này
từ đâu tới" (khác bản trước dùng `BaseHTTPRequestHandler` + soi header, từng
gây lỗi định tuyến sai dẫn tới 401 âm thầm).

## Vì sao cần "rewrites" trong vercel.json

Mặc định, Vercel map **mỗi file trong `api/`** thành 1 route trùng tên file
— file `api/index.py` chỉ tự động nhận request tại `/api/index`, KHÔNG nhận
được ở `/api` hay `/api/webhook`. Nhưng code Flask bên trong lại định nghĩa
route là `/api` và `/api/webhook` (không phải `/api/index`).

`vercel.json` giải quyết lệch pha này bằng `rewrites`: mọi request tới
`/api` hoặc `/api/webhook` sẽ được Vercel **âm thầm chuyển tiếp** cho
function `/api/index` xử lý — nhưng Flask bên trong vẫn thấy đúng
`request.path` gốc (`/api` hoặc `/api/webhook`) nên `@app.route(...)` khớp
đúng. Thiếu khối `rewrites` này, gọi `/api` hay `/api/webhook` sẽ ra `404`
ngay từ tầng Vercel, code Python còn chưa kịp chạy.

## 2 route — 2 nguồn gọi tới

| Route | Method | Ai gọi | Gửi Telegram? |
|---|---|---|---|
| `/api` | GET, POST | **cron-job.org**, mỗi 5 phút | **Có** — chỉ khi `\|z\| >= SIGNAL_THRESHOLD` và net kỳ vọng > 0 (tin "PAIRS SIGNAL") |
| `/api/webhook` | POST | **Telegram**, tự động mỗi khi có tin nhắn mới trong chat | **Có** — nếu tin nhắn là `/check`, luôn trả lời "[CHECK] PAIRS STATUS" ngay vào đúng chat |

## Logic báo tín hiệu

1. Lấy giá hiện tại của 2 leg → tính spread hiện tại.
2. So spread với `SPREAD_MEAN` / `SPREAD_STD` **cố định** (không tính lại
   rolling mỗi lần chạy) → ra z-score.
3. **Route `/api`**: nếu `|z| < SIGNAL_THRESHOLD` → dừng, không tốn thêm API
   call funding. **Route `/api/webhook` (lệnh `/check`)**: luôn tính funding
   tham khảo dù `|z|` chưa vượt ngưỡng.
4. Nếu `|z| >= SIGNAL_THRESHOLD` → gọi thêm API funding rate của cả 2 leg,
   quy đổi ra chi phí funding **mỗi ngày** cho vị thế dự kiến, nhân với số
   ngày hold kỳ vọng (`EXPECTED_HOLD_DAYS`).
5. So sánh:
   ```
   Net kỳ vọng = Lợi nhuận kỳ vọng (spread hồi mean) − Phí trade − Chi phí funding kỳ vọng
   ```
   Chỉ route `/api` (không phải `/check`) mới quyết định có gửi tín hiệu
   "vào lệnh" hay không dựa trên Net kỳ vọng > 0.

Các lệnh gọi API tới Hyperliquid (giá leg A, giá leg B, funding) chạy
**song song** (thread pool) thay vì tuần tự, giảm thời gian chờ tối đa và
tránh timeout function trên Vercel — quan trọng nhất ở `/check` vì luôn gọi
đủ cả 3.

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
code hay redeploy.

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
- Vercel đọc `vercel.json`, tự áp dụng `rewrites` cho `/api` và `/api/webhook`.
- Vào **Project Settings → Environment Variables**, thêm:

  | Key | Value |
  |---|---|
  | `TELEGRAM_BOT_TOKEN` | token từ BotFather |
  | `TELEGRAM_CHAT_ID` | chat id của bạn |
  | `CRON_SECRET` | chuỗi ngẫu nhiên ≥16 ký tự tự đặt (bảo vệ route `/api`) |
  | `TELEGRAM_WEBHOOK_SECRET` | chuỗi ngẫu nhiên ≥16 ký tự khác (bảo vệ route `/api/webhook` — xem bước 5) |
  | `SPREAD_MEAN` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SPREAD_STD` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SIGNAL_THRESHOLD` | để trống nếu dùng mặc định 1.5 |
  | `EXPECTED_HOLD_DAYS` | để trống nếu dùng mặc định |

- Deploy. Copy URL project, VD: `https://your-project.vercel.app`.

### 4. Cấu hình cron-job.org (route `/api`)
- Đăng ký/đăng nhập [cron-job.org](https://cron-job.org).
- **Create cronjob**:
  - **URL**: `https://your-project.vercel.app/api`
  - **Schedule**: Every 5 minutes (`*/5 * * * *`)
  - **Request method**: **POST**
  - **Headers**: `Authorization: Bearer <CRON_SECRET>` (đúng giá trị đã set trên Vercel)
  - **Notifications**: nên bật "notify on failure".
  - Save & Enable.

Test tay trước khi enable:
```bash
curl -v -X POST -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api
```
Kỳ vọng JSON trả về, VD:
```json
{"z": -0.42, "spread": -3.48, "should_enter": false, "reason": "z-score dưới ngưỡng"}
```

### 5. Đăng ký Telegram Webhook (route `/api/webhook`)

Bước này khiến Telegram tự động POST tới `/api/webhook` mỗi khi bạn gõ tin
nhắn cho bot — không có bước này, gõ `/check` trong Telegram sẽ không có
phản hồi gì (tin nhắn chỉ nằm im, không ai xử lý).

1. Gọi Telegram API `setWebhook` (chạy 1 lần từ máy bạn, không phải trên Vercel):
   ```bash
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://your-project.vercel.app/api/webhook",
       "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
     }'
   ```
   Thay `<TELEGRAM_BOT_TOKEN>` và `<TELEGRAM_WEBHOOK_SECRET>` đúng giá trị đã
   set ở bước 3. Kết quả mong đợi: `{"ok":true,"result":true,"description":
   "Webhook was set"}`.

2. Kiểm tra webhook đã đăng ký đúng chưa:
   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
   ```
   Xem `"url"` phải là `.../api/webhook` (không phải `/api` hay `/api/index`),
   và `last_error_message` (nếu có) để biết Telegram có báo lỗi gì không.

3. Mở chat Telegram với bot, gõ `/check` — phải thấy tin "[CHECK] PAIRS
   STATUS" trả lời trong vài giây. Gõ `/start` hoặc `/help` để xem hướng dẫn
   ngắn.

⚠️ Webhook chỉ trả lời đúng chat có `chat.id` khớp `TELEGRAM_CHAT_ID` đã cấu
hình — người khác nhắn tin cho bot sẽ bị bot im lặng bỏ qua.

**Muốn gỡ webhook**:
```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/deleteWebhook"
```

## Troubleshooting

1. **`/api` hoặc `/api/webhook` trả 404** — gần như chắc chắn là thiếu hoặc
   sai khối `rewrites` trong `vercel.json`. Kiểm tra file đã deploy đúng
   chưa (mở trực tiếp trên Vercel Dashboard → Deployments → xem source đã
   deploy có đúng `vercel.json` mới không).
2. **Xem Vercel Function Logs** — Vercel Dashboard → project → tab **Logs**
   (hoặc **Deployments → [mới nhất] → Functions**), gọi lại endpoint rồi xem
   log realtime — traceback (nếu có lỗi runtime/import) sẽ hiện ngay.
3. **Request bị timeout** — `vercel.json` set `maxDuration: 30` nhưng **gói
   Vercel Hobby giới hạn cứng ~10s/function, không thể override** dù khai
   báo `maxDuration` cao hơn. Nếu logs cho thấy `FUNCTION_INVOCATION_TIMEOUT`
   và bạn đang ở Hobby → cần nâng lên Pro.
4. **Thiếu env var** — nếu thiếu `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`,
   route `/api` trả JSON lỗi rõ ràng; route `/api/webhook` chỉ log lỗi ra
   Vercel Logs (luôn trả 200 cho Telegram theo đúng khuyến nghị của Telegram,
   không hiện lỗi trực tiếp trong chat trừ khi lỗi xảy ra sau khi đã xác
   định được `chat_id`).
5. **Telegram `/check` không phản hồi** — kiểm tra `getWebhookInfo` (bước
   5.2) xem `"url"` có đúng `/api/webhook` không và có `last_error_message`
   không, đồng thời kiểm tra `TELEGRAM_CHAT_ID` có đúng chat bạn đang nhắn
   không (webhook im lặng bỏ qua nếu sai chat id).
6. **Sai `CRON_SECRET`** (route `/api`) → trả về `401 Unauthorized` rõ ràng.

Lệnh test chuẩn để loại trừ dần (route `/api`):
```bash
curl -v -X POST -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api
```

## Giới hạn cần biết

- **Bot CHƯA có tín hiệu đóng lệnh tự động.** Chỉ đánh giá "có nên vào lệnh
  không" mỗi lần chạy, không biết bạn đang có vị thế mở hay không. Tin nhắn
  tín hiệu (và `/check`) có kèm dòng **"🎯 Gợi ý đóng lệnh"** — mức spread
  ứng với `EXIT_Z_THRESHOLD` (mặc định z=0) — nhưng chỉ là **con số tham
  khảo tại thời điểm vào lệnh**, không phải cảnh báo tự động khi giá thực sự
  chạm mức đó. Cần thêm state lưu vị thế đang mở (Vercel KV/Upstash Redis)
  để làm được — báo tôi nếu muốn triển khai tiếp.
- **Route `/api` vẫn stateless** cho phần vào lệnh — mỗi lần chạy tự đánh
  giá lại từ đầu, không nhớ "đã có vị thế đang mở" hay chưa. Nếu điều kiện
  vào lệnh tiếp tục đúng trong nhiều chu kỳ 5 phút liên tiếp, bạn sẽ nhận
  tín hiệu lặp lại nhiều lần. Cần thêm state để chỉ bắn khi **chuyển trạng
  thái** (flat → in-signal) — báo tôi nếu muốn bổ sung.
- Telegram `/check` chưa có cache/rate-limit riêng — gõ liên tục nhiều lần
  trong thời gian ngắn sẽ gọi Hyperliquid API nhiều lần tương ứng.
- Funding rate lấy tại **thời điểm hiện tại**, không phải trung bình dự kiến
  suốt thời gian hold — chi phí thực tế có thể khác số ước tính lúc vào lệnh.
- Đây là **bot báo tín hiệu**, không tự đặt lệnh.
- Backtest Task 3 chỉ dựa trên **52 ngày dữ liệu** (chưa đủ 90 ngày) và
  **không tính funding rate lịch sử lẫn slippage** — kết quả live thực tế
  nhiều khả năng khác so với số backtest. Nên theo dõi sát 2-4 tuần đầu
  trước khi tăng size.