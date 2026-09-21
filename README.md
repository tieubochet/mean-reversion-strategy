# Pairs Trading Signal Bot — Multi-pair (Hyperliquid HIP-3)

Bot tín hiệu mean-reversion theo dõi **3 cặp** trên Hyperliquid (`xyz:*`), tính
z-score + Net PnL (phí, không gồm funding), phân vùng MID/FULL, rồi gửi Telegram.

Hỗ trợ quét tự động theo lịch (cron mỗi 5 phút → `/api`) và lệnh trong chat
(`/check`, `/entry`, `/start`, `/help`).

**Đây là bot báo tín hiệu, không tự đặt lệnh.** Không lưu vị thế đang mở.

## Cấu trúc project

```
telegram-pairs-bot/
├── api/
│   └── index.py      # 1 FILE Flask — toàn bộ logic + 2 nhóm route
├── vercel.json       # builds + routes trỏ /api và /api/webhook → api/index.py
├── requirements.txt  # requests, flask
└── README.md
```

Flask tự định tuyến theo `request.path` thật. Không đoán nguồn request từ header
(bản cũ dùng `BaseHTTPRequestHandler` từng route sai → 401 âm thầm).

## Routing trên Vercel

File `api/index.py` mặc định chỉ được Vercel bind vào path trùng tên file
(`/api/index`). `vercel.json` hiện tại dùng **builds + routes** (không dùng
`rewrites`) để chuyển `/api` và `/api/webhook` vào cùng function:

```json
{
  "version": 2,
  "builds": [
    { "src": "api/index.py", "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/api/webhook", "dest": "api/index.py" },
    { "src": "/api", "dest": "api/index.py" }
  ]
}
```

Trong code, Flask còn đăng ký thêm path dự phòng:

| Path Flask | Mục đích |
|---|---|
| `/`, `/api`, `/api/`, `/api/index` | cron scan |
| `/api/webhook`, `/webhook` | Telegram webhook |

Thiếu `routes` (hoặc `rewrites` tương đương) thì gọi `/api` / `/api/webhook`
sẽ 404 ngay tầng Vercel, Python chưa chạy.

## 2 nguồn gọi

| Route | Method | Ai gọi | Gửi Telegram? |
|---|---|---|---|
| `/api` | GET, POST | **cron-job.org**, mỗi 5 phút | **Luôn** — tin `*[SCAN]*` cho **mọi** cặp (kể cả khi chưa đủ ngưỡng) |
| `/api/webhook` | POST | **Telegram** khi có tin nhắn | Có — tùy lệnh |

Auth:

- `/api`: header `Authorization: Bearer <CRON_SECRET>`
- `/api/webhook`: header `X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>`
  (Telegram gửi khi `setWebhook` có `secret_token`)

Nếu env secret để trống, route đó **không kiểm tra auth** (chỉ dùng lúc dev).

## Cặp đang theo dõi

| `pair_id` | Label | Leg A | Leg B | Spread |
|---|---|---|---|---|
| `cl` | CL/BRENT | `xyz:CL` | `xyz:BRENTOIL` | `price_A − price_B` ($/bbl) |
| `xyz100` | XYZ100/SP500 | `xyz:XYZ100` | `xyz:SP500` | `ln(A / B)` |
| `goldsilver` | GOLD/SILVER | `xyz:GOLD` | `xyz:SILVER` | `ln(A / B)` |

Cặp XAU / Variational **đã code sẵn nhưng đang tắt**.

Giá lấy từ Hyperliquid `candleSnapshot` interval `15m` (close nến mới nhất),
2 leg chạy song song (`ThreadPoolExecutor`) để giảm timeout Vercel Hobby (~10s).

## Logic tín hiệu

1. Lấy giá 2 leg → tính spread theo `spread_type` của cặp.
2. Z-score với **mean/std cố định** (không rolling mỗi lần chạy):

   ```
   z = (spread − mean) / std
   ```

3. Expected PnL nếu spread hồi về mean:
   - **diff** (CL): `Δspread × (capital / avg_price)` — số thùng đổi theo giá dầu.
   - **log-ratio** (xyz100, goldsilver): `Δln × capital_per_leg` — giả định
     dollar-neutral $1:$1 **tại entry**, không re-hedge khi hold.

4. Phí round-trip:

   ```
   fee = (FEE_BPS_PER_FILL / 10_000) × capital_per_leg × FILLS_PER_ROUND
   ```

   Mặc định `2.2 bps × 4 fills`. **Không trừ funding** (đã gỡ API funding).

   ```
   Net PnL = expected PnL − fee
   ```

5. Vùng MID / FULL (dùng cho dòng action trên Telegram):

   | Zone | Điều kiện |
   |---|---|
   | **MID** | `\|z\| ≥ mid_z` |
   | **FULL** | `\|z\| ≥ full_z` **và** spread cách một trong các mốc `mean ± full_z·std`, `range_min`, `range_max` trong `full_near_pct` (~3%) |
   | không vào | còn lại → `CHƯA NÊN VÀO` |

   Với log-ratio, “gần mốc 3%” so theo `abs(spread − level) ≤ ln(1.03)`,
   không phải % của giá trị log.

6. `should_enter` (JSON cron) = `|z| ≥ threshold` **và** `net_expected > 0`.
   Tin Telegram **không** ẩn cặp khi chưa đủ điều kiện — cron luôn gửi đủ 3 cặp.

Hướng vị thế trên tin nhắn:

- `z > 0` → SHORT A / LONG B (spread đang cao hơn mean)
- `z < 0` → LONG A / SHORT B

Mốc đóng tham khảo: spread tại `exit_z` (mặc định 0 = về đúng mean).

## Thông số mặc định trong `index.py`

Mọi số đều override được bằng env var suffix `_CL` / `_XYZ100` / `_GOLDSILVER`.

### CL / BRENT (`spread = A − B`)

| Tham số | Default | Env |
|---|---|---|
| Mean | `-4.1789` | `SPREAD_MEAN_CL` |
| Std | `0.8487` | `SPREAD_STD_CL` |
| Threshold / mid_z | `1.3` | `SIGNAL_THRESHOLD_CL`, `MID_Z_CL` |
| full_z | `2.0` | `FULL_Z_CL` |
| full_near_pct | `0.03` | `FULL_NEAR_PCT_CL` |
| Range | `[-6.009, -1.897]` | `RANGE_MIN_CL`, `RANGE_MAX_CL` |
| Exit z | `0.0` | `EXIT_Z_THRESHOLD_CL` |
| Hold TB | `270h` | `EXPECTED_HOLD_DAYS_CL` (ngày) |
| Hold FULL | `292h` | `FULL_HOLD_HOURS_CL` |
| Capital / leg | `$5000` | `CAPITAL_PER_LEG_CL` |

### XYZ100 / SP500 (`ln(A/B)`)

| Tham số | Default | Env |
|---|---|---|
| Mean | `1.352480` | `SPREAD_MEAN_XYZ100` |
| Std | `0.019752` | `SPREAD_STD_XYZ100` |
| Threshold / mid_z | `1.5` | `SIGNAL_THRESHOLD_XYZ100`, `MID_Z_XYZ100` |
| full_z | `2.1` | `FULL_Z_XYZ100` |
| Range | `[1.3093, 1.4034]` | `RANGE_MIN_XYZ100`, `RANGE_MAX_XYZ100` |
| Hold TB / FULL | `566.5h` / `549h` | `EXPECTED_HOLD_DAYS_XYZ100`, `FULL_HOLD_HOURS_XYZ100` |
| Capital / leg | `$5000` | `CAPITAL_PER_LEG_XYZ100` |

### GOLD / SILVER (`ln(A/B)`)

| Tham số | Default | Env |
|---|---|---|
| Mean | `4.220096` | `SPREAD_MEAN_GOLDSILVER` |
| Std | `0.024484` | `SPREAD_STD_GOLDSILVER` |
| Threshold / mid_z | `1.4` | `SIGNAL_THRESHOLD_GOLDSILVER`, `MID_Z_GOLDSILVER` |
| full_z | `2.5` | `FULL_Z_GOLDSILVER` |
| Range | `[4.1438, 4.2829]` | `RANGE_MIN_GOLDSILVER`, `RANGE_MAX_GOLDSILVER` |
| Hold TB / FULL | `227h` / `282.5h` | `EXPECTED_HOLD_DAYS_GOLDSILVER`, `FULL_HOLD_HOURS_GOLDSILVER` |
| Capital / leg | `$5000` | `CAPITAL_PER_LEG_GOLDSILVER` |

Chung:

| Env | Default |
|---|---|
| `FEE_BPS_PER_FILL` | `2.2` |
| `FILLS_PER_ROUND` | `4` |

Đổi mean/std/threshold trên Vercel Environment Variables — **không cần redeploy**.

## Lệnh Telegram

| Lệnh | Việc làm |
|---|---|
| `/start`, `/help` | Danh sách cặp + hướng dẫn |
| `/check` | Trạng thái **cả 3** cặp lúc này |
| `/check cl` / `xyz100` / `goldsilver` | 1 cặp |
| `/entry` | Quy tắc vào lệnh discretionary (Net PnL + DCA), **không** phải output của `evaluate_signal` |

Webhook chỉ trả lời chat có `chat.id` khớp `TELEGRAM_CHAT_ID`. Chat khác bị bỏ qua (vẫn HTTP 200).

### Quy tắc `/entry` (vốn tham chiếu $5k/leg, DCA 2k/leg × 4–5 nhịp)

Đây là rule discretionary hard-code trong `PAIRS_TEXT`, tách khỏi zone MID/FULL:

| Cặp | Vào | Dải Net quan sát |
|---|---|---|
| CL/BRENT | LONG BRENT / SHORT CL khi Net **≤ 60**; ngược lại khi Net **≥ 90** | ~30–150 |
| XYZ100 (QQQ) / SP500 | LONG QQQ / SHORT US500 khi Net **≥ 190**; ngược lại khi Net **≤ 160** | ~120–350 |
| GOLD/SILVER | LONG GOLD / SHORT SILVER khi Net **≥ 150**; ngược lại khi Net **≤ 50** | ~20–200 |

Nhịp DCA gợi ý: CL ~10 giá / nhịp; XYZ100 ~20–30; GOLD/SILVER ~35–45.

## Setup

### 1. Telegram bot

- [@BotFather](https://t.me/BotFather) → `/newbot` → `TELEGRAM_BOT_TOKEN`
- Nhắn 1 tin cho bot, mở `https://api.telegram.org/bot<TOKEN>/getUpdates` lấy `chat.id` → `TELEGRAM_CHAT_ID`

### 2. Push GitHub + deploy Vercel

```bash
git init
git add .
git commit -m "init pairs trading signal bot"
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

Vercel → New Project → import repo. Env vars bắt buộc:

| Key | Ý nghĩa |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token BotFather |
| `TELEGRAM_CHAT_ID` | chat nhận tin |
| `CRON_SECRET` | ≥16 ký tự, bảo vệ `/api` |
| `TELEGRAM_WEBHOOK_SECRET` | ≥16 ký tự khác, bảo vệ webhook |

Các `SPREAD_*`, `MID_Z_*`, `FULL_Z_*`, `RANGE_*`, `EXPECTED_HOLD_DAYS_*`,
`FULL_HOLD_HOURS_*`, `CAPITAL_PER_LEG_*` để trống nếu dùng default trong code.

### 3. cron-job.org → `/api`

- URL: `https://<project>.vercel.app/api`
- Schedule: `*/5 * * * *`
- Method: **POST**
- Header: `Authorization: Bearer <CRON_SECRET>`
- Bật notify on failure

Test:

```bash
curl -v -X POST -H "Authorization: Bearer <CRON_SECRET>" \
  https://<project>.vercel.app/api
```

Kỳ vọng JSON `{"results": {...}, "errors": {...}}` và tin `*[SCAN]*` trên Telegram.

### 4. Đăng ký Telegram webhook (chạy 1 lần trên máy local)

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<project>.vercel.app/api/webhook",
    "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
  }'
```

Kỳ vọng: `{"ok":true,"result":true,"description":"Webhook was set"}`.

Kiểm tra:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

`"url"` phải là `.../api/webhook` (không phải `/api` hay `/api/index`).

Gỡ webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/deleteWebhook"
```

## Recalibrate

1. Chạy lại script thống kê spread trên cửa sổ dữ liệu mới (vd. 90 ngày M15).
2. Update env: `SPREAD_MEAN_<X>`, `SPREAD_STD_<X>`, và nếu cần `MID_Z_*`,
   `FULL_Z_*`, `RANGE_MIN_*` / `RANGE_MAX_*`, `EXPECTED_HOLD_DAYS_*`,
   `FULL_HOLD_HOURS_*`.
3. Không cần redeploy — request kế tiếp đọc env mới.

Gợi ý: theo dõi 2–4 tuần live bằng `/check` trước khi tăng size, đặc biệt
log-ratio (xyz100, goldsilver) vì PnL mô hình không re-hedge.

## Troubleshooting

1. **`/api` hoặc `/api/webhook` 404** — sai/thiếu `routes` trong `vercel.json`,
   hoặc deploy chưa gồm file mới. Dashboard → Deployments → Source.
2. **Function logs** — Vercel → Logs. Traceback import/runtime hiện ở đây.
3. **Timeout** — Hobby cứng ~10s/function dù khai `maxDuration`. Hai call
   candle đã song song; nếu vẫn timeout (mạng HL chậm) cần Pro hoặc giảm việc
   trong 1 invocation.
4. **Thiếu token/chat_id** — `/api` trả JSON lỗi; webhook vẫn HTTP 200
   (Telegram yêu cầu) và chỉ log trên Vercel, trừ khi đã biết `chat_id` rồi
   gửi tin lỗi vào chat.
5. **`/check` im lặng** — `getWebhookInfo` sai URL / `last_error_message`;
   hoặc `TELEGRAM_CHAT_ID` không khớp chat đang gõ.
6. **Sai `CRON_SECRET`** — `/api` trả `401 Unauthorized`.
7. **Markdown Telegram vỡ** — tin dùng `parse_mode: Markdown`; ký tự đặc biệt
   trong exception message có thể làm Telegram từ chối gửi (xem log).

## Giới hạn đã chấp nhận

- Không tín hiệu **đóng lệnh** tự động. Dòng “Đóng khi spread về …” chỉ là
  mốc tính tại lúc scan, không phải alert khi giá chạm.
- Không state vị thế. Cron stateless — điều kiện còn đúng thì `[SCAN]` lặp
  mỗi 5 phút. Muốn chỉ bắn lúc flat → in-signal cần KV/Redis.
- `/check` không cache; spam lệnh = spam API Hyperliquid.
- Funding **không** vào Net. Chi phí funding live có thể làm đảo dấu so với số bot.
- Log-ratio: hedge $1:$1 chỉ đúng lúc entry. Giá chạy mạnh → tỷ trọng lệch,
  sai số này chưa được backtest mô phỏng intra-hold.
- CL Net đổi theo giá dầu vì `units = capital / avg_price`.
- Backtest nguồn các mean/std/hold không gồm funding lịch sử lẫn slippage.
- Variational listings cache 8s trong process — hiện không dùng vì cặp XAU tắt.
