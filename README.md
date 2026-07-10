# Pairs Trading Signal Bot — WTI (xyz:CL) vs Brent (xyz:BRENTOIL)

Bot theo dõi z-score của spread WTI-Brent trên Hyperliquid, kiểm tra funding
rate trước khi báo tín hiệu, và gửi vào Telegram khi đủ điều kiện. Hỗ trợ cả
quét tự động theo lịch (cron) lẫn quét theo yêu cầu ngay trong chat Telegram
(gõ `/check`).

## Cấu trúc project

```
telegram-pairs-bot/
├── api/
│   └── index.py    # 1 FILE DUY NHẤT — toàn bộ logic: config, fetch data,
│                     # tính z-score, funding cost, gửi Telegram, VÀ xử lý
│                     # cả 2 loại request (cron-job.org lẫn Telegram webhook)
│                     # trên CÙNG 1 URL /api/index.
├── vercel.json      # maxDuration=30s (gọi nhiều API song song tới
│                      # Hyperliquid, cần hơn mức mặc định)
├── requirements.txt
└── README.md
```

Chỉ 1 file `api/index.py` — không có file phụ nào khác trong `api/`, nên
không có rủi ro lỗi import chéo giữa các Vercel Python function (mỗi file
trong `api/` được Vercel bundle thành 1 function độc lập; càng ít file càng
ít chỗ để lặp code hoặc quên đồng bộ logic giữa các file).

## 1 URL — phục vụ 2 nguồn gọi tới, tự phân biệt bằng header

`https://your-project.vercel.app/api/index` được cấu hình ở **cả 2 nơi**:

| Ai gọi | Method | Header nhận diện | Gửi Telegram? |
|---|---|---|---|
| **cron-job.org**, mỗi 5 phút | POST | `Authorization: Bearer <CRON_SECRET>` | **Có** — chỉ khi `\|z\| >= SIGNAL_THRESHOLD` và net kỳ vọng > 0 (tin "PAIRS SIGNAL") |
| **Telegram**, mỗi khi có tin nhắn mới trong chat | POST | `X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>` | **Có** — nếu tin nhắn là `/check`, luôn trả lời "[CHECK] PAIRS STATUS" ngay vào đúng chat đó |

Code tự nhận diện: nếu request có header `X-Telegram-Bot-Api-Secret-Token`
→ coi là Telegram (chỉ Telegram gửi header này, do mình khai báo `secret_token`
lúc đăng ký webhook) → xử lý như tin nhắn chat. Ngược lại → coi là cron-job.org
→ xác thực bằng `CRON_SECRET` → quét và chỉ gửi tin khi đủ điều kiện vào lệnh.

Không cần curl thủ công để xem trạng thái nữa — gõ `/check` thẳng trong
Telegram là đủ.

## Logic báo tín hiệu

1. Lấy giá hiện tại của 2 leg → tính spread hiện tại.
2. So spread với `SPREAD_MEAN` / `SPREAD_STD` **cố định** (không tính lại
   rolling mỗi lần chạy) → ra z-score.
3. **Nhánh cron**: nếu `|z| < SIGNAL_THRESHOLD` → dừng, không tốn thêm API
   call funding. **Nhánh Telegram `/check`**: luôn tính funding tham khảo dù
   `|z|` chưa vượt ngưỡng, để bạn chủ động xem funding hiện tại đang "ăn" bao
   nhiêu vào lợi nhuận kỳ vọng.
4. Nếu `|z| >= SIGNAL_THRESHOLD` → gọi thêm API funding rate của cả 2 leg,
   quy đổi ra chi phí funding **mỗi ngày** cho vị thế dự kiến, nhân với số
   ngày hold kỳ vọng (`EXPECTED_HOLD_DAYS`).
5. So sánh:
   ```
   Net kỳ vọng = Lợi nhuận kỳ vọng (spread hồi mean) − Phí trade − Chi phí funding kỳ vọng
   ```
   Chỉ nhánh cron (không phải `/check`) mới quyết định có gửi tín hiệu "vào
   lệnh" hay không dựa trên Net kỳ vọng > 0.

Các lệnh gọi API tới Hyperliquid (giá leg A, giá leg B, funding) được chạy
**song song** (thread pool) thay vì tuần tự, để giảm thời gian chờ tối đa và
tránh timeout function trên Vercel — đặc biệt quan trọng ở nhánh `/check` vì
luôn phải gọi đủ cả 3.

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
- Vercel tự nhận `api/index.py` là 1 Python serverless function.
- Vào **Project Settings → Environment Variables**, thêm:

  | Key | Value |
  |---|---|
  | `TELEGRAM_BOT_TOKEN` | token từ BotFather |
  | `TELEGRAM_CHAT_ID` | chat id của bạn |
  | `CRON_SECRET` | chuỗi ngẫu nhiên ≥16 ký tự tự đặt (bảo vệ nhánh cron) |
  | `TELEGRAM_WEBHOOK_SECRET` | chuỗi ngẫu nhiên ≥16 ký tự khác (bảo vệ nhánh Telegram — xem bước 5) |
  | `SPREAD_MEAN` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SPREAD_STD` | để trống nếu dùng mặc định, hoặc số mới sau recalibrate |
  | `SIGNAL_THRESHOLD` | để trống nếu dùng mặc định 1.5 |
  | `EXPECTED_HOLD_DAYS` | để trống nếu dùng mặc định |

- Deploy. Copy URL project, VD: `https://your-project.vercel.app`.

### 4. Cấu hình cron-job.org (nhánh quét tự động mỗi 5 phút)
- Đăng ký/đăng nhập [cron-job.org](https://cron-job.org).
- **Create cronjob**:
  - **URL**: `https://your-project.vercel.app/api/index`
  - **Schedule**: Every 5 minutes (`*/5 * * * *`)
  - **Request method**: **POST**
  - **Headers**: `Authorization: Bearer <CRON_SECRET>` (đúng giá trị đã set trên Vercel)
  - **Notifications**: nên bật "notify on failure".
  - Save & Enable.

Test tay trước khi enable:
```bash
curl -v -X POST -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api/index
```
Kỳ vọng JSON trả về, VD:
```json
{"z": -0.42, "spread": -3.48, "should_enter": false, "reason": "z-score dưới ngưỡng"}
```

### 5. Đăng ký Telegram Webhook (nhánh `/check` trong chat)

Bước này khiến Telegram tự động POST tới `/api/index` mỗi khi bạn gõ tin
nhắn cho bot — không có bước này, gõ `/check` trong Telegram sẽ không có
phản hồi gì (tin nhắn chỉ nằm im, không ai xử lý).

1. Gọi Telegram API `setWebhook` (chạy 1 lần từ máy bạn, không phải trên Vercel):
   ```bash
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://your-project.vercel.app/api/index",
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
   Xem `url` khớp đúng project, và `last_error_message` (nếu có) để biết
   Telegram có báo lỗi gì khi thử gọi webhook không.

3. Mở chat Telegram với bot, gõ `/check` — phải thấy tin "[CHECK] PAIRS
   STATUS" trả lời trong vài giây. Gõ `/start` hoặc `/help` để xem hướng dẫn
   ngắn.

⚠️ Webhook chỉ trả lời đúng chat có `chat.id` khớp `TELEGRAM_CHAT_ID` đã cấu
hình — người khác nhắn tin cho bot (nếu họ biết username bot) sẽ bị bot im
lặng bỏ qua, không tốn API quota Hyperliquid.

**Muốn gỡ webhook** (vd để bot ngừng phản hồi tin nhắn Telegram, chỉ còn
chạy cron):
```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/deleteWebhook"
```

## Troubleshooting: gọi endpoint mà không thấy gì trả về

1. **Đã deploy đúng chưa / đúng URL chưa** — mở
   `https://your-project.vercel.app/api/index` trực tiếp trên trình duyệt
   (GET), xem có JSON trả về không.
2. **Xem Vercel Function Logs** — vào project trên Vercel → tab **Logs** (hoặc
   **Deployments → [deployment mới nhất] → Functions**), gọi lại endpoint rồi
   xem log realtime. Cách chẩn đoán nhanh và chính xác nhất, thường hiện rõ
   traceback nếu là lỗi import/runtime.
3. **Request bị timeout** — function gọi 2-3 API tới Hyperliquid (đã chạy
   song song để giảm thời gian chờ). `vercel.json` set `maxDuration: 30`
   nhưng **gói Vercel Hobby giới hạn cứng ~10s/function, không thể override
   bằng `maxDuration`** dù khai báo trong `vercel.json`. Nếu logs cho thấy
   `FUNCTION_INVOCATION_TIMEOUT` và bạn đang ở Hobby → cần nâng lên Pro.
4. **Thiếu env var** — nếu thiếu `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`,
   function vẫn nên trả JSON lỗi rõ ràng (`{"error": "Missing ..."}`), không
   phải im lặng.
5. **Telegram `/check` không phản hồi trong chat** — kiểm tra `getWebhookInfo`
   (bước 5.2 ở trên) xem Telegram có báo lỗi khi gọi webhook không, và kiểm
   tra `TELEGRAM_CHAT_ID` có đúng chat bạn đang nhắn không (webhook im lặng
   bỏ qua nếu sai chat id).
6. **Sai `CRON_SECRET`** (nhánh cron) → trả về `401 Unauthorized` (có body,
   không phải im lặng).

Lệnh test chuẩn để loại trừ dần (nhánh cron):
```bash
curl -v -X POST -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api/index
```
Cờ `-v` giúp thấy rõ status code + response headers, phân biệt được "hoàn
toàn không có response" (timeout/network) với "có response nhưng rỗng/lỗi".

## Giới hạn cần biết

- **Bot CHƯA có tín hiệu đóng lệnh tự động.** Chỉ đánh giá "có nên vào lệnh
  không" mỗi lần chạy, không biết bạn đang có vị thế mở hay không. Tin nhắn
  tín hiệu (và `/check`) có kèm dòng **"🎯 Gợi ý đóng lệnh"** — mức spread
  ứng với `EXIT_Z_THRESHOLD` (mặc định z=0, tức khi spread hồi đúng về Mean,
  giống quy tắc exit trong backtest Task 3) — nhưng đây chỉ là **con số tham
  khảo tại thời điểm vào lệnh**, không phải cảnh báo tự động khi giá thực sự
  chạm mức đó. Bạn cần tự theo dõi bằng cách gõ lại `/check` định kỳ, hoặc
  đặt sẵn take-profit/limit order trên sàn ngay khi vào lệnh.
  Muốn có thông báo tự động **khi thực sự tới điểm đóng** (không chỉ gợi ý
  lúc vào lệnh), cần thêm state lưu vị thế đang mở (Vercel KV/Upstash Redis)
  — báo tôi nếu bạn muốn triển khai tiếp phần này.
- **Nhánh cron vẫn stateless** cho phần vào lệnh — mỗi lần chạy tự đánh giá
  lại từ đầu, không nhớ "đã có vị thế đang mở" hay chưa. Nếu điều kiện vào
  lệnh tiếp tục đúng trong nhiều chu kỳ 5 phút liên tiếp, bạn sẽ nhận tín
  hiệu lặp lại nhiều lần. Muốn tránh spam, cần thêm state (Vercel KV hoặc
  Upstash Redis) để chỉ bắn khi **chuyển trạng thái** (flat → in-signal).
  Báo tôi nếu muốn bổ sung phần này.
- Telegram `/check` chưa có cache/rate-limit riêng — gõ `/check` liên tục
  nhiều lần trong thời gian ngắn sẽ gọi Hyperliquid API nhiều lần tương ứng.
- Funding rate lấy tại **thời điểm hiện tại**, không phải trung bình dự kiến
  suốt thời gian hold — chi phí thực tế có thể khác số ước tính lúc vào lệnh.
- Đây là **bot báo tín hiệu**, không tự đặt lệnh.
- Backtest Task 3 chỉ dựa trên **52 ngày dữ liệu** (chưa đủ 90 ngày) và
  **không tính funding rate lịch sử lẫn slippage** — kết quả live thực tế
  nhiều khả năng khác so với số backtest. Nên theo dõi sát 2-4 tuần đầu
  trước khi tăng size.