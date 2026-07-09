# Pairs Trading Signal Bot — WTI (xyz:CL) vs Brent (xyz:BRENTOIL)

Bot theo dõi z-score của spread WTI-Brent trên Hyperliquid, kiểm tra funding
rate trước khi báo tín hiệu, và gửi vào Telegram khi đủ điều kiện. Scheduler
dùng **cron-job.org** gọi endpoint mỗi 5 phút.

Toàn bộ config + logic gom trong **1 file duy nhất**: `api/index.py`.

## Cấu trúc project

```
telegram-pairs-bot/
├── api/
│   └── index.py        # TOÀN BỘ bot: config, fetch data, tính z-score,
│                        # check funding cost, gửi Telegram, HTTP handler
├── requirements.txt
└── README.md
```

Endpoint sau khi deploy: `https://<your-project>.vercel.app/api/index`

## Logic báo tín hiệu (đã có kiểm tra funding rate)

Mỗi lần cron-job.org gọi endpoint:

1. Lấy giá hiện tại của 2 leg → tính spread hiện tại.
2. So spread với `SPREAD_MEAN` / `SPREAD_STD` **cố định** (không tính lại
   rolling mỗi lần chạy) → ra z-score.
3. Nếu `|z| < SIGNAL_THRESHOLD` → dừng, không làm gì thêm.
4. Nếu `|z| >= SIGNAL_THRESHOLD` → mới gọi thêm API lấy **funding rate hiện
   tại** của cả 2 leg (Hyperliquid trả/thu funding mỗi giờ), quy đổi ra chi
   phí funding **mỗi ngày** cho vị thế dự kiến, rồi nhân với số ngày hold kỳ
   vọng (`EXPECTED_HOLD_DAYS`, mặc định lấy từ hold time trung bình của
   backtest threshold=1.5 trong Task 3).
5. So sánh:
   ```
   Net kỳ vọng = Lợi nhuận kỳ vọng (spread hồi mean) − Phí trade − Chi phí funding kỳ vọng
   ```
   **Chỉ gửi tín hiệu Telegram nếu Net kỳ vọng > 0** — nếu funding ăn hết lợi
   nhuận kỳ vọng thì bot tự bỏ qua, không báo vào lệnh.

Tin nhắn Telegram hiển thị đầy đủ: z-score, spread, lợi nhuận kỳ vọng, phí
trade, funding/ngày, tổng funding cost ước tính, và net kỳ vọng cuối cùng —
để bạn nhìn số là hiểu ngay vì sao bot bắn (hoặc sẽ bắn) tín hiệu.

## Thông số đã chốt từ backtest (Task 2 & 3)

| Env var | Giá trị mặc định | Nguồn |
|---|---|---|
| `SPREAD_MEAN` | -3.2858 | Task 2, 52 ngày data |
| `SPREAD_STD` | 0.4675 | Task 2, 52 ngày data |
| `SIGNAL_THRESHOLD` | 1.5 | Task 3, winrate 89.3%, 121 trades |
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
5. Không cần redeploy — Vercel áp dụng env var mới ngay cho lần gọi cron kế tiếp.

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
- Vercel tự nhận `api/index.py` là Python serverless function.
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
curl -H "Authorization: Bearer <CRON_SECRET>" https://your-project.vercel.app/api/index
```
Kỳ vọng trả về JSON, VD:
```json
{"z": -0.42, "spread": -3.48, "should_enter": false, "reason": "z-score dưới ngưỡng"}
```
hoặc khi z vượt ngưỡng nhưng funding cost quá cao:
```json
{"z": 1.8, "spread": -2.44, "should_enter": false, "reason": "Funding cost + phí ăn hết lợi nhuận kỳ vọng -> bỏ qua", "net_expected": -12.3}
```
Nếu lỗi 401 → sai `CRON_SECRET`; lỗi 500 → xem message trong JSON (thường do
thiếu env var hoặc Hyperliquid API đổi format).

### 5. Cấu hình cron-job.org
- Đăng ký/đăng nhập [cron-job.org](https://cron-job.org).
- **Create cronjob**:
  - **URL**: `https://your-project.vercel.app/api/index`
  - **Schedule**: Every 5 minutes (`*/5 * * * *`)
  - **Request method**: GET
  - **Headers**: `Authorization: Bearer <CRON_SECRET>` (đúng giá trị đã set trên Vercel)
  - **Notifications**: nên bật "notify on failure".
  - Save & Enable.

## Giới hạn cần biết

- **Bot vẫn stateless** — mỗi lần cron chạy tự đánh giá lại từ đầu, không nhớ
  "đã có vị thế đang mở" hay chưa. Nếu điều kiện vào lệnh (z vượt ngưỡng VÀ
  net kỳ vọng dương) tiếp tục đúng trong nhiều chu kỳ 5 phút liên tiếp, bạn
  sẽ nhận **tín hiệu lặp lại** nhiều lần thay vì chỉ 1 lần khi vừa đủ điều
  kiện. Muốn tránh spam, cần thêm state lưu trữ (Vercel KV hoặc Upstash
  Redis) để chỉ bắn tín hiệu khi **chuyển trạng thái** (flat → in-signal).
  Báo tôi nếu bạn muốn bổ sung phần này.
- Funding rate lấy tại **thời điểm hiện tại**, không phải trung bình dự kiến
  suốt thời gian hold — nếu funding rate biến động mạnh trong lúc giữ lệnh,
  chi phí thực tế có thể khác số ước tính lúc vào lệnh.
- Đây là **bot báo tín hiệu**, không tự đặt lệnh. Vào/ra lệnh vẫn do bạn thực
  hiện thủ công, hoặc tự nối thêm phần gọi Hyperliquid order API (cần ký
  request bằng private key — rủi ro bảo mật cao hơn nhiều so với bot chỉ đọc
  dữ liệu, cân nhắc kỹ trước khi tự động hoá đặt lệnh).
- Backtest Task 3 chỉ dựa trên **52 ngày dữ liệu** (chưa đủ 90 ngày) và
  **không tính funding rate lịch sử lẫn slippage** — kết quả live thực tế
  nhiều khả năng khác so với số backtest. Nên theo dõi sát 2-4 tuần đầu
  trước khi tăng size.
