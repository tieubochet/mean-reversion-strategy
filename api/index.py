import os
import requests
import telebot
from fastapi import FastAPI, Request, BackgroundTasks

app = FastAPI()

# ==================== 1. CẤU HÌNH CHIẾN LƯỢC ====================
VON_PER_LEG = 14000         # $14,000/leg
GIA_WTI_TRUNG_BINH = 70.0   # Giá dầu cơ sở
BARRELS = VON_PER_LEG / GIA_WTI_TRUNG_BINH # ~200 barrels

THRESHOLD_SHORT_SPREAD = -2.90  
THRESHOLD_LONG_SPREAD = -4.10   
MEAN_SPREAD = -3.50             
MAX_ACCEPTABLE_FUNDING_PAY = 0.015 / 100 

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") 

# Bật lại threaded=True nhưng xử lý qua luồng background để không làm nghẽn FastAPI
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True) if TELEGRAM_TOKEN else None
HL_API_URL = "https://api.hyperliquid.xyz/info"

# ==================== 2. LẤY DATA TỪ DEX XYZ ====================
def get_hl_market_data():
    headers = {"Content-Type": "application/json"}
    try:
        mids_resp = requests.post(HL_API_URL, headers=headers, json={"type": "allMids", "dex": "xyz"}, timeout=8).json()
        meta_resp = requests.post(HL_API_URL, headers=headers, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=8).json()
        
        if isinstance(mids_resp, dict) and isinstance(meta_resp, list) and len(meta_resp) >= 2:
            wti_price = float(mids_resp.get("CL", 0))
            brent_price = float(mids_resp.get("BRENTOIL", 0))
            
            if wti_price == 0 or brent_price == 0:
                return None
                
            wti_funding = 0.0
            brent_funding = 0.0
            
            universe = meta_resp[0].get("universe", [])
            asset_ctxs = meta_resp[1]
            for i, asset in enumerate(universe):
                name = asset.get("name", "").upper()
                if i < len(asset_ctxs):
                    if name == "CL":
                        wti_funding = float(asset_ctxs[i].get("funding", 0))
                    elif name == "BRENTOIL":
                        brent_funding = float(asset_ctxs[i].get("funding", 0))
                        
            return {
                "wti": {"price": wti_price, "funding": wti_funding},
                "brent": {"price": brent_price, "funding": brent_funding}
            }
    except Exception as e:
        print(f"Lỗi kết nối hoặc xử lý data HL: {e}")
    return None

# ==================== 3. LOGIC TÍNH TOÁN TIN NHẮN ====================
def build_signal_report(is_manual_check=False):
    data = get_hl_market_data()
    if not data:
        return "❌ Lỗi: Không thể kết nối hoặc phân tích dữ liệu phân vùng DEX `xyz` từ Hyperliquid.", False

    wti_p = data["wti"]["price"]
    wti_f = data["wti"]["funding"]
    brent_p = data["brent"]["price"]
    brent_f = data["brent"]["funding"]
    
    current_spread = wti_p - brent_p
    signal = None
    action_wti, action_brent = "", ""
    net_funding_hourly = 0.0

    if current_spread >= THRESHOLD_SHORT_SPREAD:
        signal = "SHORT SPREAD (Co hẹp)"
        action_wti, action_brent = "SHORT 🔴", "LONG 🟢"
        net_funding_hourly = (-wti_f) + brent_f
    elif current_spread <= THRESHOLD_LONG_SPREAD:
        signal = "LONG SPREAD (Dãn rộng)"
        action_wti, action_brent = "LONG 🟢", "SHORT 🔴"
        net_funding_hourly = wti_f - brent_f

    funding_status_text = "N/A"
    is_funding_ok = False
    
    if signal:
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

    title = "🔍 *KIỂM TRA TÍN HIỆU CHỦ ĐỘNG*" if is_manual_check else "🚨 *CẢNH BÁO TÍN HIỆU MEAN REVERSION*"
    
    if signal:
        aspread_target = abs(current_spread - MEAN_SPREAD)
        estimated_pnl = aspread_target * BARRELS
        
        msg = (
            f"{title}\n"
            f"─────────────────────\n"
            f"💡 **Chiến lược:** {signal}\n"
            f"📊 **Spread hiện tại:** `${current_spread:.2f}`\n"
            f"🎯 **Target về Mean:** `${MEAN_SPREAD:.2f}` (Lợi nhuận: `+{aspread_target:.2f}$/bbl`)\n\n"
            f"📋 **Hành động (Vốn ${VON_PER_LEG:,}/leg ~ {BARRELS:.0f} bbls):**\n"
            f"  • WTI (xyz:CL): {action_wti} | Giá: `${wti_p:.2f}`\n"
            f"  • Brent (xyz:BRENTOIL): {action_brent} | Giá: `${brent_p:.2f}`\n\n"
            f"💸 **Trạng thái Funding dòng:**\n"
            f"  • {funding_status_text}\n"
            f"💰 **Ước tính Gross PnL vòng này:** `+{estimated_pnl:.2f}$`"
        )
        return msg, is_funding_ok
    else:
        msg = (
            f"{title}\n"
            f"─────────────────────\n"
            f"📊 **Spread hiện tại:** `${current_spread:.2f}`\n"
            f"  • Giá WTI: `${wti_p:.2f}` | Funding: `{wti_f*100:+.4f}%/h`\n"
            f"  • Giá Brent: `${brent_p:.2f}` | Funding: `{brent_f*100:+.4f}%/h`\n"
            f"🎯 **Biên kích hoạt:** Long $\le$ `{THRESHOLD_LONG_SPREAD}` | Short $\ge$ `{THRESHOLD_SHORT_SPREAD}`\n"
            f"⏳ **Trạng thái:** Trong vùng trung tính - Chưa kích hoạt lệnh."
        )
        return msg, False

# ==================== 4. ENDPOINTS ====================

@app.get("/api")
@app.post("/api")
def cron_scan():
    """Dành cho Cron-job ngoài quét ngầm (Chỉ bắn tin khi thỏa mãn cả 2 điều kiện)"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return {"status": "error", "message": "Missing environment variables"}
    
    msg, should_trigger = build_signal_report(is_manual_check=False)
    if should_trigger:
        try:
            bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
            return {"status": "success", "triggered": True}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "success", "triggered": False}

if bot:
    @bot.message_handler(commands=['check'])
    def telegram_check(message):
        """Xử lý lệnh /check"""
        chat_id = str(message.chat.id)
        try:
            msg, _ = build_signal_report(is_manual_check=True)
            bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Lỗi: {str(e)}")

# Hàm xử lý gói update chạy ngầm, giải phóng block luồng cho FastAPI
def process_telegram_updates(json_data: dict):
    if bot:
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Cổng nhận dữ liệu từ Webhook Telegram - Sử dụng BackgroundTasks để chống nghẽn"""
    if bot:
        json_data = await request.json()
        # Đẩy việc xử lý tin nhắn vào hàng đợi chạy ngầm và trả về phản hồi 'OK' ngay lập tức cho Telegram
        background_tasks.add_task(process_telegram_updates, json_data)
    return "OK"

@app.get("/")
def index():
    return {"status": "online"}