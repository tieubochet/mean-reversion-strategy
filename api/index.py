import os
import requests
import telebot
from fastapi import FastAPI, Request

app = FastAPI()

# --- BIẾN MÔI TRƯỜNG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

THRESHOLD_SHORT_SPREAD = -2.90
THRESHOLD_LONG_SPREAD = -4.10
MEAN_SPREAD = -3.50
MAX_ACCEPTABLE_FUNDING_PAY = 0.015 / 100 

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False) if TELEGRAM_TOKEN else None
HL_API_URL = "https://api.hyperliquid.xyz/info"

def get_hl_market_data():
    # Sử dụng endpoint UI thường ít bị Cloudflare siết IP hơn endpoint gốc
    url = "https://api-ui.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    try:
        # 1. Lấy giá midPrice của tất cả các cặp (bản tin này rất nhẹ)
        price_payload = {"type": "allMids"}
        price_res = requests.post(url, json=price_payload, timeout=5)
        
        # 2. Lấy danh sách funding hiện tại
        funding_payload = {"type": "metaAndAssetCtxs"}
        funding_res = requests.post(url, json=funding_payload, timeout=5)
        
        if price_res.status_code == 200 and funding_res.status_code == 200:
            prices = price_res.json()
            funding_data = funding_res.json()
            
            wti_name = "xyz:CL"
            brent_name = "xyz:BRENTOIL"
            
            if wti_name not in prices or brent_name not in prices:
                return None
                
            result = {
                wti_name: {"price": float(prices[wti_name]), "funding": 0.0},
                brent_name: {"price": float(prices[brent_name]), "funding": 0.0}
            }
            
            # Trích xuất funding từ cấu trúc meta
            universe = funding_data[0]['universe']
            asset_ctxs = funding_data[1]
            for index, asset in enumerate(universe):
                name = asset['name']
                if name in [wti_name, brent_name]:
                    result[name]["funding"] = float(asset_ctxs[index]['funding'])
                    
            return result
    except Exception as e:
        print(f"Lỗi kết nối Hyperliquid: {e}")
    return None

def build_signal_message(is_manual_check=False):
    """Hàm xử lý logic tính toán và dựng tin nhắn báo cáo"""
    data = get_hl_market_data()
    if not data or "xyz:CL" not in data or "xyz:BRENTOIL" not in data:
        return "❌ Không thể kết nối hoặc lấy dữ liệu từ Hyperliquid.", False

    wti_price = data["xyz:CL"]["price"]
    wti_funding = data["xyz:CL"]["funding"]
    brent_price = data["xyz:BRENTOIL"]["price"]
    brent_funding = data["xyz:BRENTOIL"]["funding"]
    
    current_spread = wti_price - brent_price
    signal = None
    action_wti, action_brent = "", ""
    net_funding_hourly = 0.0

    if current_spread >= THRESHOLD_SHORT_SPREAD:
        signal = "SHORT SPREAD (Co hẹp)"
        action_wti, action_brent = "SHORT 🔴", "LONG 🟢"
        net_funding_hourly = (-wti_funding) + brent_funding
    elif current_spread <= THRESHOLD_LONG_SPREAD:
        signal = "LONG SPREAD (Dãn rộng)"
        action_wti, action_brent = "LONG 🟢", "SHORT 🔴"
        net_funding_hourly = wti_funding - brent_funding

    # Xử lý hiển thị Funding
    if signal:
        if net_funding_hourly >= 0:
            funding_ok = True
            funding_text = f"🟢 CÓ LỢI (Nhận +{net_funding_hourly*100:.4f}%/h)"
        else:
            cost = abs(net_funding_hourly)
            funding_ok = cost <= MAX_ACCEPTABLE_FUNDING_PAY
            funding_text = f"🟡 CHẤP NHẬN ĐƯỢC (Trả -{cost*100:.4f}%/h)" if funding_ok else f"❌ BẤT LỢI QUÁ MỨC (Trả -{cost*100:.4f}%/h) -> Bỏ qua"
    else:
        funding_ok = False
        funding_text = "N/A (Chưa có vị thế)"

    # Khởi tạo tiêu đề tùy thuộc vào phương thức gọi
    title = "🔍 **KIỂM TRA TÍN HIỆU CHỦ ĐỘNG** 🔍" if is_manual_check else "🚨 **TÍN HIỆU GIAO DỊCH SPREAD ĐẦU THÔ** 🚨"

    # Định dạng tin nhắn gửi về
    if signal:
        message = (
            f"{title}\n\n"
            f"💡 **Chiến lược:** {signal}\n"
            f"📊 **Spread hiện tại:** ${current_spread:.2f}\n"
            f"🎯 **Target về Mean:** ${MEAN_SPREAD:.2f}\n\n"
            f"📋 **Hành động:**\n"
            f"   • WTI (xyz:CL): {action_wti} | Giá: ${wti_price:.2f}\n"
            f"   • Brent (xyz:BRENTOIL): {action_brent} | Giá: ${brent_price:.2f}\n\n"
            f"💸 **Funding:** {funding_text}"
        )
        return message, funding_ok
    else:
        message = (
            f"{title}\n\n"
            f"📊 **Spread hiện tại:** ${current_spread:.2f}\n"
            f"🎯 **Biên kích hoạt:** Long $\le$ {THRESHOLD_LONG_SPREAD} | Short $\ge$ {THRESHOLD_SHORT_SPREAD}\n"
            f"💤 **Trạng thái:** Thị trường chưa có tín hiệu vào lệnh tốt."
        )
        return message, False

# --- 1. ENDPOINT CHO CRON-JOB (Chỉ bắn tin nhắn khi ĐẠT ĐỦ ĐIỀU KIỆN) ---
def run_cron_flow():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return {"status": "error", "message": "Missing environment variables"}
    
    msg, should_trigger = build_signal_message(is_manual_check=False)
    if should_trigger:
        try:
            bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
            return {"status": "success", "sent": True}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "checked", "sent": False}

@app.get("/cron/check-spread")
def cron_get(): return run_cron_flow()

@app.post("/cron/check-spread")
def cron_post(): return run_cron_flow()

# --- 2. ENDPOINT TELEGRAM WEBHOOK (Gõ lệnh /check là TRẢ LỜI NGAY, bất kể có signal hay không) ---
if bot:
    @bot.message_handler(commands=['check'])
    def handle_check_command(message):
        # Tạo tin nhắn kiểm tra (is_manual_check=True)
        msg, _ = build_signal_message(is_manual_check=True)
        bot.reply_to(message, msg, parse_mode="Markdown")

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    """Endpoint tiếp nhận bản tin cập nhật từ Telegram"""
    if bot:
        json_string = await request.json()
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
    return {"status": "ok"}

@app.get("/")
def index():
    return {"status": "bot is online"}