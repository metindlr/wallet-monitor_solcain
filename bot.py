import os
import time
import threading
from flask import Flask
import telebot
import requests

# --- AYARLAR VE ENVIRONMENT VARIABLES ---
# Render üzerinde bu Environment Variable'ları tanımlamalısın
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Bildirimlerin geleceği Telegram User ID'niz

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Cüzdan verilerini hafızada tutuyoruz (Render ücretsiz planda disk sıfırlandığı için basitlik adına RAM'de)
# Gerçekçi senaryoda bir veritabanı gerekir ancak geçici/hızlı çözümler için yeterlidir.
tracked_wallets = {}  # { "wallet_address": { "token_mint": miktar } }
wallet_nicknames = {} # { "wallet_address": "Nickname" }

# Sabit Eşik Değeri (5,000 USD)
MIN_USD_VALUE = 5000.0

def get_wallet_portfolio(address):
    """Helius Wallet API kullanarak cüzdanın 5000$ üstü tokenlarını ve toplam değerini getirir."""
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
                # Sadece 5000$ ve üzeri yatırımları filtrele
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

# --- TELEGRAM BOT KOMUTLARI ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🧠 *Solana Cüzdan İzleme Botuna Hoş Geldin!*\n\n"
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
        
        bot.reply_to(message, f"🔍 `{address}` inceleniyor ve portföy hesaplanıyor...", parse_mode="Markdown")
        
        total_usd, tokens = get_wallet_portfolio(address)
        
        if total_usd is None:
            bot.reply_to(message, "❌ Helius API'den veri alınamadı. Adresi kontrol edin.")
            return
            
        # Hafızaya kaydet
        tracked_wallets[address] = {mint: info["amount"] for mint, info in tokens.items()}
        wallet_nicknames[address] = nickname
        
        # Yanıt mesajı hazırlama
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

# --- ARKA PLAN TAKİP DÖNGÜSÜ (TRACKER) ---

def tracker_loop():
    """Cüzdanları düzenli aralıklarla sorgular, alım/satım ve yeni eklenen tokenları yakalar."""
    while True:
        # Cüzdan listesi boşsa bekle
        if not tracked_wallets:
            time.sleep(10)
            continue
            
        for address, old_tokens in list(tracked_wallets.items()):
            nickname = wallet_nicknames.get(address, address[:6])
            total_usd, current_tokens = get_wallet_portfolio(address)
            
            if total_usd is None:
                continue  # Bu turda API hatası alındıysa atla
                
            # 1. Yeni Eklenen Token Kontrolü (Önceden yoktu veya 5k barajını yeni geçti)
            for mint, info in current_tokens.items():
                if mint not in old_tokens:
                    alert_msg = (
                        f"🚨 *YENİ TOKEN POZİSYONU!* (>{int(MIN_USD_VALUE/1000)}k$)\n"
                        f"👤 *Cüzdan:* {nickname}\n"
                        f"🪙 *Token:* {info['symbol']}\n"
                        f"💰 *Yatırım Değeri:* ${info['usd_value']:,.2f}\n"
                        f"📊 *Cüzdan Toplamı:* ${total_usd:,.2f}"
                    )
                    try:
                        bot.send_message(ADMIN_CHAT_ID, alert_msg, parse_mode="Markdown")
                    except Exception as e:
                        print(e)
                
                # 2. Alım / Satım Kontrolü (Mevcut token miktarındaki değişim)
                else:
                    old_amount = old_tokens[mint]
                    new_amount = info["amount"]
                    diff = new_amount - old_amount
                    
                    if abs(diff) / old_amount > 0.01:  # %1'den büyük bir miktar değişimi varsa bildir
                        action = "🟢 ALIM YAPTI" if diff > 0 else "🔴 SATIM YAPTI"
                        tx_msg = (
                            f"🐳 *BALİNA HAREKETİ!* [{action}]\n"
                            f"👤 *Cüzdan:* {nickname}\n"
                            f"🪙 *Token:* {info['symbol']}\n"
                            f"📈 *Miktar Değişimi:* {abs(diff):,.2f}\n"
                            f"💵 *Güncel Pozisyon Değeri:* ${info['usd_value']:,.2f}\n"
                            f"📊 *Cüzdan Toplamı:* ${total_usd:,.2f}"
                        )
                        try:
                            bot.send_message(ADMIN_CHAT_ID, tx_msg, parse_mode="Markdown")
                        except Exception as e:
                            print(e)

            # Hafızadaki durumu güncelle
            tracked_wallets[address] = {mint: info["amount"] for mint, info in current_tokens.items()}
            time.sleep(2)  # Rate limit yememek için cüzdanlar arası kısa bekleme
            
        time.sleep(60)  # Her 1 dakikada bir cüzdanları baştan tara

# --- RENDER İÇİN WEB SERVER ---

@app.route('/')
def home():
    return "Bot is alive!", 200

def run_flask():
    # Render PORT env variable sağlar, yoksa 8080 varsayılan olur
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    # Arka plan tarayıcısını başlat
    t_tracker = threading.Thread(target=tracker_loop, daemon=True)
    t_tracker.start()
    
    # Telegram bot polling'i arka planda başlat
    t_bot = threading.Thread(target=lambda: bot.infinity_polling(), daemon=True)
    t_bot.start()
    
    # Ana thread'de Flask web sunucusunu çalıştır (Render'ın kapanmaması için şart)
    run_flask()
