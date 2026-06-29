import os
import requests
import telebot
from fastapi import FastAPI, Response, status

app = FastAPI()

# --- CẤU HÌNH CƠ SỞ (Lấy từ Environment Variables trên Vercel để bảo mật) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# Ngưỡng kích hoạt cho khung ngắn (đã tối ưu theo nến 15m)
THRESHOLD_SHORT_SPREAD = -2.90
THRESHOLD_LONG_SPREAD = -4.10
MEAN_SPREAD = -3.50
MAX_ACCEPTABLE_FUNDING_PAY = 0.015 / 100 

bot = telebot.TeleBot(TELEGRAM_TOKEN)
HL_API_URL = "https://api.hyperliquid.xyz/info"

def get_hl_market_data():
    payload = {"type": "metaAndAssetCtxs"}
    try:
        response = requests.post(HL_API_URL, json=payload, timeout=8)
        if response.status_code == 200:
            data = response.json()
            universe = data[0]['universe']
            asset_ctxs = data[1]
            
            result = {}
            for index, asset in enumerate(universe):
                name = asset['name']
                if name in ["xyz:CL", "xyz:BRENTOIL"]:
                    ctx = asset_ctxs[index]
                    result[name] = {
                        "price": float(ctx['midPx']),
                        "funding": float(ctx['funding']) 
                    }
            return result
    except Exception as e:
        print(f"Lỗi API HL: {e}")
    return None

# Endpoint này sẽ được Cron-job gọi đến mỗi 5 phút
@app.get("/cron/check-spread")
def check_market_cron():
    data = get_hl_market_data()
    if not data or "xyz:CL" not in data or "xyz:BRENTOIL" not in data:
        return {"status": "error", "message": "Failed to fetch data from Hyperliquid"}

    wti_price = data["xyz:CL"]["price"]
    wti_funding = data["xyz:CL"]["funding"]
    brent_price = data["xyz:BRENTOIL"]["price"]
    brent_funding = data["xyz:BRENTOIL"]["funding"]
    
    current_spread = wti_price - brent_price
    signal = None
    action_wti, action_brent = "", ""
    net_funding_hourly = 0.0

    # Kiểm tra điều kiện Spread
    if current_spread >= THRESHOLD_SHORT_SPREAD:
        signal = "SHORT SPREAD (Co hẹp)"
        action_wti, action_brent = "SHORT 🔴", "LONG 🟢"
        net_funding_hourly = (-wti_funding) + brent_funding
    elif current_spread <= THRESHOLD_LONG_SPREAD:
        signal = "LONG SPREAD (Dãn rộng)"
        action_wti, action_brent = "LONG 🟢", "SHORT 🔴"
        net_funding_hourly = wti_funding - brent_funding

    # Kiểm tra điều kiện Funding
    if signal:
        is_funding_ok = False
        funding_status_text = ""

        if net_funding_hourly >= 0:
            is_funding_ok = True
            funding_status_text = f"🟢 CÓ LỢI (Nhận +{net_funding_hourly*100:.4f}%/h)"
        else:
            cost = abs(net_funding_hourly)
            if cost <= MAX_ACCEPTABLE_FUNDING_PAY:
                is_funding_ok = True
                funding_status_text = f"🟡 CHẤP NHẬN ĐƯỢC (Trả -{cost*100:.4f}%/h)"
            else:
                funding_status_text = f"❌ BẤT LỢI QUÁ MỨC (Trả -{cost*100:.4f}%/h) -> Bỏ qua"

        if is_funding_ok:
            message = (
                f"🚨 **TÍN HIỆU GIAO DỊCH SPREAD ĐẦU THÔ (5M CRON)** 🚨\n\n"
                f"💡 **Chiến lược:** {signal}\n"
                f"📊 **Spread hiện tại:** ${current_spread:.2f}\n"
                f"🎯 **Target về Mean:** ${MEAN_SPREAD:.2f}\n\n"
                f"📋 **Hành động:**\n"
                f"   • WTI (xyz:CL): {action_wti} | Giá: ${wti_price:.2f}\n"
                f"   • Brent (xyz:BRENTOIL): {action_brent} | Giá: ${brent_price:.2f}\n\n"
                f"💸 **Funding:** {funding_status_text}"
            )
            try:
                bot.send_message(CHAT_ID, message, parse_mode="Markdown")
                return {"status": "success", "signal_triggered": True, "spread": current_spread}
            except Exception as e:
                return {"status": "error", "message": f"Telegram failed: {str(e)}"}
        else:
            return {"status": "ignored", "reason": "Funding unfavourable", "spread": current_spread}
            
    return {"status": "checked", "signal_triggered": False, "spread": current_spread}

@app.get("/")
def index():
    return {"status": "bot is running"}