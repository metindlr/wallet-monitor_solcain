import os
import time
import threading
from flask import Flask, request
import requests

# --- AYARLAR VE ENVIRONMENT VARIABLES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# Solscan Pro API anahtarınız varsa buraya ekleyin, yoksa boş bırakın (Bot public endpoint deneyecek)
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "") 
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

app = Flask(__name__)

tracked_wallets = {}  # { "address": { "mint": amount } }
wallet_nicknames = {} # { "address": "Nickname" }
MIN_USD_VALUE = 5000.0

def send_telegram_message(chat_id, text):
    """Telegram üzerinden mesaj gönderir."""
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

def get_wallet_portfolio(address):
    """Doğrudan Solscan API kullanarak portföy ve token değerlerini çeker."""
    # Solscan v2 API Account/Tokens endpoint
    url = f"https://pro-api.solscan.io/v2/account/tokens?address={address}"
    headers = {
        "token": SOLSCAN_API_KEY if SOLSCAN_API_KEY else "public" # API Key yoksa public fallback dener
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        
        # Eğer Pro API rate limit veya auth hatası verirse, genel Solscan public web-api'sine fallback yapıyoruz
        if response.status_code != 200:
            url = f"https://api.solscan.io/account/tokens?address={address}"
            response = requests.get(url, timeout=15)
            
        if response.status_code == 200:
            res_data = response.json()
            # Solscan veri yapısında data listesi gelir
            token_list = res_data.get("data", [])
            
            calculated_total_usd = 0.0
            filtered_tokens = {}
            
            for item in token_list:
                # Solscan verilerinde miktar ve USD değerleri hazır gelir
                amount = float(item.get("tokenAmount", {}).get("uiAmount", 0))
                usd_val = float(item.get("usdAmount", 0) or item.get("value", 0))
                symbol = item.get("tokenSymbol") or item.get("tokenName") or "UNKNOWN"
                mint = item.get("tokenAddress") or item.get("mint", "")
                
                if amount <= 0:
                    continue
                    
                calculated_total_usd += usd_val
                
                # Solscan'in kendi hesapladığı USD değeri 5000$'dan büyükse filtreye al
                if usd_val >= MIN_USD_VALUE:
                    filtered_tokens[mint] = {
                        "symbol": symbol,
                        "amount": amount,
                        "usd_value": usd_val
                    }
            
            # Eğer Solscan total account value değerini ana objede taşıyorsa onu al, yoksa toplama güven
            total_usd = res_data.get("totalUsd", calculated_total_usd)
            if total_usd == 0:
                total_usd = calculated_total_usd
                
            return total_usd, filtered_tokens
            
    except Exception as e:
        print(f"Solscan API Hatası ({address}): {e}")
        
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
                "🧠 *Solscan Tabanlı Cüzdan İzleme Botu Aktif!*\n\n"
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
            
            send_telegram_message(chat_id, f"🔍 `{address}` Solscan üzerinden sorgulanıyor...")
            
            total_usd, tokens = get_wallet_portfolio(address)
            if total_usd is None:
                send_telegram_message(chat_id, "❌ Solscan API'den veri alınamadı.")
                return "OK", 200
                
            tracked_wallets[address] = {mint: info["amount"] for mint, info in tokens.items()}
            wallet_nicknames[address] = nickname
            
            msg = f"✅ *Cüzdan Başarıyla Eklendi!*\n👤 *İsim:* {nickname}\n💰 *Toplam Portföy (Solscan):* ${total_usd:,.2f}\n\n*🐋 5,000$ Üzeri Yatırımlar:*\n"
            if not tokens:
                msg += "_Bu cüzdanda Solscan verilerine göre 5,000$ üzerinde yatırım yapılan token bulunmuyor._"
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
    return "Bot is live with Solscan API!", 200

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
                        f"📊 *Solscan Toplamı:* ${total_usd:,.2f}"
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
                            f"📊 *Solscan Toplamı:* ${total_usd:,.2f}"
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
