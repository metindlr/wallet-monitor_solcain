import os
import time
import threading
from flask import Flask, request
import telebot
import requests

# --- AYARLAR VE ENVIRONMENT VARIABLES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
# Render'ın sana verdiği URL (Örn: https://botun.onrender.com)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") 

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

tracked_wallets = {}  
wallet_nicknames = {} 
MIN_USD_VALUE = 5000.0

def get_wallet_portfolio(address):
    url = f"https://api.helius.xyz/v1/wallet/{address}/balances?api-key={HELIUS_API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            total_usd = data.get("totalUsdValue", 0.0)
            balances = data.get("balances", [])
            
            filtered_tokens = {}
            for item in balances:
                usd_val = item.get("usdAmount", 0.0)
                if usd_val >= MIN_USD_VALUE:
                    mint = item.get("mint", "SOL")
                    symbol = item.get("tokenSymbol", "UNKNOWN")
                    amount = item.get("amount", 0)
                    decimals = item.get("decimals", 9)
                    clean_amount = amount / (10 ** decimals)
                    
                    filtered_tokens[mint] = {
                        "symbol": symbol,
                        "amount": clean_amount,
                        "usd_value": usd_val
                    }
            return total_usd, filtered_tokens
    except Exception as e:
        print(f"Helius API Hatası ({address}): {e}")
    return None, None

# --- WEBHOOK ENDPOINT ---
@app.route('/' + TELEGRAM_TOKEN, methods=['POST'])
def get_message():
    """Telegram'dan gelen mesajları karşılayan webhook endpoint'i"""
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@app.route('/')
def home():
    """Uptime robot için ana sayfa"""
    return "Bot is alive and watching!", 200

# --- TELEGRAM BOT KOMUTLARI ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🧠 *Solana Cüzdan İzleme Botu Aktif!*\n\n"
        "Komutlar:\n"
        "`/ekle <cüzdan_adresi> <takma_ad>` - Listeye cüzdan ekler.\n"
        "`/listele` - Takip edilen cüzdanları gösterir.\n"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['ekle'])
def add_wallet(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "⚠️ Kullanım: `/ekle <cüzdan_adresi> <takma_ad>`", parse_mode="Markdown")
            return
        
        address = args[1]
        nickname = args[2] if len(args) > 2 else address[:6]
        
        bot.reply_to(message, f"🔍 `{address}` inceleniyor...", parse_mode="Markdown")
        
        total_usd, tokens = get_wallet_portfolio(address)
        
        if total_usd is None:
            bot.reply_to(message, "❌ Helius API'den veri alınamadı.")
            return
            
        tracked_wallets[address] = {mint: info["amount"] for mint, info in tokens.items()}
        wallet_nicknames[address] = nickname
        
        msg = f"✅ *Cüzdan Başarıyla Eklendi!*\n👤 *İsim:* {nickname}\n💰 *Toplam Portföy:* ${total_usd:,.2f}\n\n*🐋 5,000$ Üzeri Yatırımlar:*\n"
        if not tokens:
            msg += "_Bu cüzdanda 5,000$ üzerinde token bulunmuyor._"
        for mint, info in tokens.items():
            msg += f"• *{info['symbol']}:* {info['amount']:,.2f} (${info['usd_value']:,.2f})\n"
            
        bot.reply_to(message, msg, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"Bir hata oluştu: {e}")

@bot.message_handler(commands=['listele'])
def list_wallets(message):
    if not tracked_wallets:
        bot.reply_to(message, "Takip edilen cüzdan bulunmuyor.")
        return
    msg = "📋 *Takip Edilen Cüzdanlar:*\n\n"
    for addr, nickname in wallet_nicknames.items():
        msg += f"• *{nickname}:* `{addr}`\n"
    bot.reply_to(message, msg, parse_mode="Markdown")

# --- ARKA PLAN TAKİP DÖNGÜSÜ ---
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
                    )
                    try: bot.send_message(ADMIN_CHAT_ID, alert_msg, parse_mode="Markdown")
                    except: pass
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
                            f"💵 *Güncel Pozisyon Değeri:* ${info['usd_value']:,.2f}"
                        )
                        try: bot.send_message(ADMIN_CHAT_ID, tx_msg, parse_mode="Markdown")
                        except: pass

            tracked_wallets[address] = {mint: info["amount"] for mint, info in current_tokens.items()}
            time.sleep(2)
        time.sleep(60)

# --- WEBHOOK SETTINGS ON START ---
# Gunicorn başlatıldığında webhook'u Telegram'a kaydeder
if RENDER_EXTERNAL_URL:
    bot.remove_webhook()
    time.sleep(1)
    # Telegram'a mesajları bu URL'e post etmesini söylüyoruz
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
    bot.set_webhook(url=webhook_url)
    print(f"Webhook başarıyla ayarlandı: {webhook_url}")

# Arka plan takibini başlat
t_tracker = threading.Thread(target=tracker_loop, daemon=True)
t_tracker.start()

# Flask uygulamasını gunicorn ayağa kaldıracak, if __name__ == '__main__': kısmına gerek kalmadı.
