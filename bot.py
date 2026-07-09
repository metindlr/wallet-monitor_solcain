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

def get_multiple_token_prices(mint_addresses):
    """Verilen tüm token mint adreslerinin fiyatlarını tek seferde Jupiter'den çeker."""
    if not mint_addresses:
        return {}
    try:
        ids_str = ",".join(mint_addresses)
        url = f"https://api.jup.ag/price/v2?ids={ids_str}"
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            res_data = response.json()
            prices = {}
            for mint in mint_addresses:
                price_val = res_data.get("data", {}).get(mint, {}).get("price")
                prices[mint] = float(price_val) if price_val else 0.0
            return prices
    except Exception as e:
        print(f"Jupiter Toplu Fiyat Sorgulama Hatası: {e}")
    return {}

def get_wallet_portfolio(address):
    """Helius'tan sadece adetleri alır, tüm fiyatlandırmayı Jupiter ile CANLI yapar."""
    url = f"https://api.helius.xyz/v1/wallet/{address}/balances?api-key={HELIUS_API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            return None, None
            
        data = response.json()
        balances = data.get("balances", [])
        
        if not balances:
            return 0.0, {}

        # 1. Aşama: Cüzdandaki tüm tokenları tara ve mint adreslerini topla
        mint_addresses = [item.get("mint") for item in balances if item.get("mint")]
        
        # 2. Aşama: Jupiter'den tüm bu tokenların gerçek ve canlı fiyatlarını tek bir istekte çek
        jupiter_prices = get_multiple_token_prices(mint_addresses)
        
        calculated_total_usd = 0.0
        filtered_tokens = {}
        
        # 3. Aşama: Gerçek fiyatlarla bakiye hesaplaması yap
        for item in balances:
            mint = item.get("mint", "SOL")
            amount = item.get("amount", 0)
            decimals = item.get("decimals", 9)
            clean_amount = amount / (10 ** decimals)
            symbol = item.get("tokenSymbol") or item.get("symbol") or "UNKNOWN"
            
            if clean_amount <= 0:
                continue
                
            # Fiyatı kesinlikle Jupiter'den al, eğer orada yoksa Helius'un verdiğine güven (en son çare)
            token_price = jupiter_prices.get(mint, 0.0)
            if token_price == 0.0:
                token_price = item.get("price") or item.get("tokenPrice") or 0.0
                
            # Gerçek anlık dolar değerini hesapla
            usd_val = clean_amount * token_price
            calculated_total_usd += usd_val
            
            # Tamamen dolar odaklı filtreleme: Adete bakmaksızın değeri 5,000$ üzerindeyse listele
            if usd_val >= MIN_USD_VALUE:
                filtered_tokens[mint] = {
                    "symbol": symbol,
                    "amount": clean_amount,
                    "usd_value": usd_val
                }
                
        return calculated_total_usd, filtered_tokens
            
    except Exception as e:
        print(f"Portföy Hesaplama Hatası ({address}): {e}")
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
                send_telegram_message(chat_id, "❌ Veri hesaplanamadı. Lütfen adresi kontrol edin.")
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
