import os
import time
import threading
from flask import Flask, request
import requests

# --- AYARLAR VE ENVIRONMENT VARIABLES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

app = Flask(__name__)

# Küresel hafıza (Hafızada cüzdan takibi için)
tracked_wallets = {}  # { "address": { "mint": amount } }
wallet_nicknames = {} # { "address": "Nickname" }
MIN_USD_VALUE = 5000.0

def send_telegram_message(chat_id, text):
    """Doğrudan Telegram HTTP API üzerinden mesaj gönderir."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"Telegram Gönderim Durumu: {res.status_code}")
    except Exception as e:
        print(f"Telegram Mesaj Gönderme Hatası: {e}")

def get_token_price_fallback(mint_address):
    """Helius veya Jupiter üzerinden tokenın anlık birim dolar fiyatını sorgular."""
    try:
        # Jupiter Fiyat API'si Solana ağındaki tüm tokenların fiyatını doğrulamak için en güvenilir yoldur
        url = f"https://api.jup.ag/price/v2?ids={mint_address}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            res_data = response.json()
            price = res_data.get("data", {}).get(mint_address, {}).get("price")
            if price:
                return float(price)
    except Exception as e:
        print(f"Yedek Fiyat Sorgulama Hatası ({mint_address}): {e}")
    return 0.0

def get_wallet_portfolio(address):
    """Helius API'den cüzdanı çeker, fiyatı eksik tokenların değerini dinamik hesaplar."""
    url = f"https://api.helius.xyz/v1/wallet/{address}/balances?api-key={HELIUS_API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            
            calculated_total_usd = 0.0
            balances = data.get("balances", [])
            filtered_tokens = {}
            
            for item in balances:
                usd_val = item.get("usdAmount") or item.get("usd_amount") or 0.0
                
                amount = item.get("amount", 0)
                decimals = item.get("decimals", 9)
                clean_amount = amount / (10 ** decimals)
                symbol = item.get("tokenSymbol") or item.get("symbol") or "UNKNOWN"
                mint = item.get("mint", "SOL")
                
                # --- SAF DOLAR DEĞERİ DOĞRULAMA KATMANI ---
                # Eğer Helius bu tokenın USD değerini sıfır döndüyse ama cüzdanda adet varsa:
                if usd_val == 0 and clean_amount > 0:
                    # Önce Helius içindeki dahili fiyatı kontrol et
                    token_price = item.get("price") or item.get("tokenPrice") or 0.0
                    
                    # Eğer dahili fiyat da sıfırsa, Jupiter API üzerinden gerçek birim fiyatı sorgula
                    if token_price == 0:
                        token_price = get_token_price_fallback(mint)
                        
                    # Bulunan fiyatla gerçek dolar değerini hesapla
                    if token_price > 0:
                        usd_val = clean_amount * token_price
                
                calculated_total_usd += usd_val
                
                # Kesinlikle sadece dolar değeri 5,000$ ve üzerinde olanları filtreye al
                if usd_val >= MIN_USD_VALUE:
                    filtered_tokens[mint] = {
                        "symbol": symbol,
                        "amount": clean_amount,
                        "usd_value": usd_val
                    }
            
            final_total = max(data.get("totalUsdValue", 0.0), calculated_total_usd)
            return final_total, filtered_tokens
            
    except Exception as e:
        print(f"Helius API Hatası ({address}): {e}")
    return None, None

# --- WEBHOOK ENDPOINT (GELEN MESAJLARI İŞLEME) ---
@app.route('/' + TELEGRAM_TOKEN, methods=['POST'])
def webhook_handler():
    try:
        data = request.get_json()
        if not data or "message" not in data:
            return "OK", 200
            
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        
        if text.startswith('/start') or text.startswith('/help'):
            help_text = (
                "🧠 *Solana Cüzdan İzleme Botu Aktif!*\n\n"
                "Komutlar:\n"
                "`/ekle <cüzdan_adresi> <takma_ad>` - Listeye cüzdan ekler.\n"
                "`/listele` - Takip edilen cüzdanları gösterir.\n"
            )
            send_telegram_message(chat_id, help_text)
            
        elif text.startswith('/ekle'):
            args = text.split()
            if len(args) < 2:
                send_telegram_message(chat_id, "⚠️ Kullanım: `/ekle <cüzdan_adresi> <takma_ad>`")
                return "OK", 200
                
            address = args[1]
            nickname = args[2] if len(args) > 2 else address[:6]
            
            send_telegram_message(chat_id, f"🔍 `{address}` inceleniyor, portföy hesaplanıyor...")
            
            total_usd, tokens = get_wallet_portfolio(address)
            if total_usd is None:
                send_telegram_message(chat_id, "❌ Helius API'den veri alınamadı.")
                return "OK", 200
                
            tracked_wallets[address] = {mint: info["amount"] for mint, info in tokens.items()}
            wallet_nicknames[address] = nickname
            
            msg = f"✅ *Cüzdan Başarıyla Eklendi!*\n👤 *İsim:* {nickname}\n💰 *Toplam Portföy:* ${total_usd:,.2f}\n\n*🐋 5,000$ Üzeri Yatırımlar:*\n"
            if not tokens:
                msg += "_Bu cüzdanda 5,000$ üzerinde yatırım yapılan token bulunmuyor._"
            for mint, info in tokens.items():
                msg += f"• *{info['symbol']}:* {info['amount']:,.2f} (${info['usd_value']:,.2f})\n"
                
            send_telegram_message(chat_id, msg)
            
        elif text.startswith('/listele'):
            if not tracked_wallets:
                send_telegram_message(chat_id, "Takip edilen cüzdan bulunmuyor.")
            else:
                msg = "📋 *Takip Edilen Cüzdanlar:*\n\n"
                for addr, nickname in wallet_nicknames.items():
                    msg += f"• *{nickname}:* `{addr}`\n"
                send_telegram_message(chat_id, msg)
                
    except Exception as e:
        print(f"Webhook İşleme Hatası: {e}")
        
    return "OK", 200

@app.route('/')
def home():
    return "Bot is safe and alive!", 200

# --- ARKA PLAN BALİNA TAKİP DÖNGÜSÜ ---
def tracker_loop():
    while True:
        if not tracked_wallets:
            time.sleep(10)
            continue
            
        for address, old_tokens in list(tracked_wallets.items()):
            nickname = wallet_nicknames.get(address, address[:6])
            total_usd, current_tokens = get_wallet_portfolio(address)
            
            if total_usd is None:
                continue
                
            for mint, info in current_tokens.items():
                if mint not in old_tokens:
                    alert_msg = (
                        f"🚨 *YENİ TOKEN POZİSYONU!*\n"
                        f"👤 *Cüzdan:* {nickname}\n"
                        f"🪙 *Token:* {info['symbol']}\n"
                        f"💰 *Yatırım Değeri:* ${info['usd_value']:,.2f}\n"
                        f"📊 *Cüzdan Toplamı:* ${total_usd:,.2f}"
                    )
                    send_telegram_message(ADMIN_CHAT_ID, alert_msg)
                else:
                    old_amount = old_tokens[mint]
                    new_amount = info["amount"]
                    diff = new_amount - old_amount
                    
                    if abs(diff) / old_amount > 0.01:
                        action = "🟢 ALIM YAPTI" if diff > 0 else "🔴 SATIM YAPTI"
                        tx_msg = (
                            f"🐳 *BALİNA HAREKETİ!* [{action}]\n"
                            f"👤 *Cüzdan:* {nickname}\n"
                            f"🪙 *Token:* {info['symbol']}\n"
                            f"📈 *Miktar Değişimi:* {abs(diff):,.2f}\n"
                            f"📊 *Cüzdan Toplamı:* ${total_usd:,.2f}"
                        )
                        send_telegram_message(ADMIN_CHAT_ID, tx_msg)

            tracked_wallets[address] = {mint: info["amount"] for mint, info in current_tokens.items()}
            time.sleep(2)
        time.sleep(60)

# --- INITIALIZATION ON START ---
if RENDER_EXTERNAL_URL and TELEGRAM_TOKEN:
    try:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
        time.sleep(1)
        res = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")
        print(f"Manuel Webhook Kurulumu: {res.text}")
    except Exception as e:
        print(f"Webhook kurulum hatası: {e}")

t_tracker = threading.Thread(target=tracker_loop, daemon=True)
t_tracker.start()
