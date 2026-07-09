import os
import time
import threading
import json
from flask import Flask, request
import requests

# --- AYARLAR VE ENVIRONMENT VARIABLES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# Yedeklerin saklanacağı Chat ID (Varsayılan olarak ADMIN_CHAT_ID)
TG_BACKUP_CHAT_ID = os.getenv("TG_BACKUP_CHAT_ID") or ADMIN_CHAT_ID

app = Flask(__name__)

# Küresel hafıza yapıları
tracked_wallets = {}  # { "address": { "mint": amount } }
wallet_nicknames = {} # { "address": "Nickname" }
MIN_USD_VALUE = 5000.0

def load_data_from_telegram():
    """Bot her başladığında Admin sohbetindeki veya yedek kanalındaki SABİTLENMİŞ mesajı okur."""
    global tracked_wallets, wallet_nicknames
    print("🔄 Telegram Sabitlenmiş Mesajından cüzdan yedekleri yükleniyor...")
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat?chat_id={TG_BACKUP_CHAT_ID}"
        res = requests.get(url, timeout=15).json()
        
        pinned_message = res.get("result", {}).get("pinned_message", {})
        text = pinned_message.get("text", "")
        
        if text.startswith("📦 [TG_BACKUP_DATA]"):
            clean_json = text.replace("📦 [TG_BACKUP_DATA]", "").strip()
            backup = json.loads(clean_json)
            tracked_wallets = backup.get("tracked_wallets", {})
            wallet_nicknames = backup.get("wallet_nicknames", {})
            print("💾 BAŞARILI: Cüzdanlar Sabitlenmiş Mesajdan Ömür Boyu Korunarak Geri Yüklendi!")
            return
        else:
            print("ℹ️ Sabitlenmiş mesajda geçerli yedek bulunamadı. İlk eklemede otomatik oluşturulacak.")
            
    except Exception as e:
        print(f"❌ Telegram Sabitli Mesaj okuma hatası: {e}")
    finally:
        if RENDER_EXTERNAL_URL:
            webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

def save_data_to_telegram():
    """Cüzdan listesi değiştiğinde mevcut sabit mesajı bulur ve düzenler ya da yeni oluşturup sabitlemeye zorlar."""
    try:
        backup_payload = {
            "tracked_wallets": tracked_wallets,
            "wallet_nicknames": wallet_nicknames
        }
        backup_text = f"📦 [TG_BACKUP_DATA]\n{json.dumps(backup_payload)}"
        
        # 1. Adım: Mevcut sabitli mesajı kontrol et
        get_chat_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat?chat_id={TG_BACKUP_CHAT_ID}"
        chat_res = requests.get(get_chat_url, timeout=10).json()
        pinned_message = chat_res.get("result", {}).get("pinned_message", {})
        pinned_text = pinned_message.get("text", "")
        pinned_msg_id = pinned_message.get("message_id")
        
        # 2. Adım: Yedek mesajı zaten sabitliyse içerik güncelle (Sohbet kirlenmez)
        if pinned_msg_id and pinned_text.startswith("📦 [TG_BACKUP_DATA]"):
            edit_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
            edit_payload = {
                "chat_id": TG_BACKUP_CHAT_ID,
                "message_id": pinned_msg_id,
                "text": backup_text
            }
            edit_res = requests.post(edit_url, json=edit_payload, timeout=10).json()
            if edit_res.get("ok"):
                print("💾 Mevcut sabitlenmiş yedek mesajı başarıyla güncellendi.")
                return
        
        # 3. Adım: Sabitli yedek yoksa yeni mesaj olarak gönder
        send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        send_payload = {
            "chat_id": TG_BACKUP_CHAT_ID,
            "text": backup_text,
            "disable_notification": True
        }
        send_res = requests.post(send_url, json=send_payload, timeout=10).json()
        new_msg_id = send_res.get("result", {}).get("message_id")
        
        if new_msg_id:
            pin_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage"
            pin_payload = {
                "chat_id": TG_BACKUP_CHAT_ID,
                "message_id": new_msg_id,
                "disable_notification": True
            }
            pin_res = requests.post(pin_url, json=pin_payload, timeout=10).json()
            
            # Eğer otomatik pinleme başarısız olursa (Telegram DM kısıtlaması yüzünden) kullanıcıyı uyar
            if not pin_res.get("ok"):
                print("⚠️ Otomatik sabitleme başarısız. Kullanıcıya manuel sabitleme uyarısı gönderiliyor.")
                send_telegram_message(TG_BACKUP_CHAT_ID, "⚠️ *Kritik Uyarı:* Bot veritabanı yedeği gönderildi ancak otomatik sabitlenemedi. Lütfen yukarıdaki `📦 [TG_BACKUP_DATA]` mesajına sağ tıklayıp *SABİTLE (PIN)* yapın. Aksi takdirde cüzdanlarınız silinebilir!")
            else:
                print("💾 Yeni yedek mesajı oluşturuldu ve otomatik sabitleme yapıldı.")
            
    except Exception as e:
        print(f"❌ Telegram Otomatik Yedekleme Hatası: {e}")

def send_telegram_message(chat_id, text):
    """Telegram HTTP API üzerinden mesaj gönderir."""
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
    """Helius DAS API'yi tüm sayfaları bitene kadar döngüyle tarar."""
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    calculated_total_usd = 0.0
    all_tokens = {}
    page = 1
    
    while True:
        payload = {
            "jsonrpc": "2.0",
            "id": f"wallet-monitor-p{page}",
            "method": "getAssetsByOwner",
            "params": {
                "ownerAddress": address,
                "page": page,
                "limit": 100,
                "displayOptions": { "showFungible": True }
            }
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code != 200:
                break
                
            res_data = response.json()
            items = res_data.get("result", {}).get("items", [])
            
            if not items:
                break
                
            for item in items:
                if item.get("interface") != "FungibleToken":
                    continue
                    
                mint = item.get("id", "")
                token_info = item.get("token_info", {})
                content = item.get("content", {})
                metadata = content.get("metadata", {})
                
                symbol = token_info.get("symbol") or metadata.get("symbol") or "UNKNOWN"
                balance = token_info.get("balance", 0)
                decimals = token_info.get("decimals", 9)
                clean_amount = float(balance) / (10 ** decimals)
                
                if clean_amount <= 0:
                    continue
                
                price_info = token_info.get("price_info", {})
                usd_val = float(price_info.get("total_price", 0.0))
                
                if usd_val == 0.0:
                    price_per_token = float(price_info.get("price_per_token", 0.0))
                    usd_val = clean_amount * price_per_token
                
                calculated_total_usd += usd_val
                
                all_tokens[mint] = {
                    "symbol": symbol,
                    "amount": clean_amount,
                    "usd_value": usd_val
                }
            
            if len(items) < 100:
                break
            page += 1
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Helius DAS API Hatası ({address}) Sayfa {page}: {e}")
            return None, None
            
    return calculated_total_usd, all_tokens

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
                "🧠 *Solana Balina Takip Botu Aktif!*\n\n"
                "⚙️ *Komutlar:*\n"
                "`/ekle <cüzdan_adresi> <takma_ad>` - Cüzdanı takibe alır.\n"
                "`/sil <cüzdan_adresi>` - Cüzdanı takipten çıkarır.\n"
                "`/listele` - Takip edilen cüzdanları ve tüm token detaylarını gösterir.\n"
            )
            send_telegram_message(chat_id, help_text)
            
        elif text.startswith('/ekle'):
            args = text.split()
            if len(args) < 2:
                send_telegram_message(chat_id, "⚠️ Kullanım: `/ekle <cüzdan_adresi> <takma_ad>`")
                return "OK", 200
                
            address = args[1]
            nickname = args[2] if len(args) > 2 else address[:6]
            
            send_telegram_message(chat_id, f"🔍 `{address}` portföyünün tüm sayfaları inceleniyor...")
            
            total_usd, all_tokens = get_wallet_portfolio(address)
            if total_usd is None:
                send_telegram_message(chat_id, "❌ Helius API bağlantı hatası.")
                return "OK", 200
                
            tracked_wallets[address] = {mint: info["amount"] for mint, info in all_tokens.items()}
            wallet_nicknames[address] = nickname
            
            save_data_to_telegram()
            
            filtered_tokens = {m: i for m, i in all_tokens.items() if i["usd_value"] >= MIN_USD_VALUE}
            
            # Alım panelinde büyükten küçüğe (reverse=True) sıralama
            sorted_tokens = sorted(filtered_tokens.items(), key=lambda item: item[1]['usd_value'], reverse=True)
            
            msg = f"✅ *Cüzdan Takibe Alındı!*\n👤 *İsim:* {nickname}\n💰 *Toplam Değer:* ${total_usd:,.2f}\n\n*🐋 Varlıklar (Büyükten Küçüğe):*\n"
            if not sorted_tokens:
                msg += "_Bu cüzdanda 5,000$ üzerinde yatırım bulunmuyor._"
            for mint, info in sorted_tokens:
                msg += f"• *{info['symbol']}:* {info['amount']:,.2f} (${info['usd_value']:,.2f})\n"
                
            send_telegram_message(chat_id, msg)
            
        elif text.startswith('/sil'):
            args = text.split()
            if len(args) < 2:
                send_telegram_message(chat_id, "⚠️ Kullanım: `/sil <cüzdan_adresi>`")
                return "OK", 200
                
            address = args[1]
            if address in tracked_wallets:
                nickname = wallet_nicknames.get(address, address[:6])
                del tracked_wallets[address]
                if address in wallet_nicknames:
                    del wallet_nicknames[address]
                
                save_data_to_telegram()
                send_telegram_message(chat_id, f"🗑️ *{nickname}* (`{address}`) başarıyla takipten çıkarıldı.")
            else:
                send_telegram_message(chat_id, "❌ Bu cüzdan zaten takip listesinde bulunmuyor.")
                
        elif text.startswith('/listele'):
            if not tracked_wallets:
                send_telegram_message(chat_id, "📭 Takip edilen cüzdan bulunmuyor.")
            else:
                msg = "📊 *TAKİP EDİLEN BALİNA CÜZDANLARI VE PORTFÖYLERİ*\n"
                
                for addr, nickname in wallet_nicknames.items():
                    total_usd, all_tokens = get_wallet_portfolio(addr)
                    
                    if total_usd is None:
                        msg += f"\n◤ ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ◥\n👤 *İsim:* {nickname}\n⚠️ _Veri çekilemedi (API Hatası)_\n◣ ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ◢\n"
                        continue
                        
                    filtered_tokens = {m: i for m, i in all_tokens.items() if i["usd_value"] >= MIN_USD_VALUE}
                    
                    # 🎯 CRITICAL UPDATE: Tokenları dolar değerine (usd_value) göre BÜYÜKTEN KÜÇÜĞE (reverse=True) sırala
                    sorted_tokens = sorted(filtered_tokens.items(), key=lambda item: item[1]['usd_value'], reverse=True)
                    
                    msg += f"\n◤ ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ◥\n"
                    msg += f"👤 *Balina:* {nickname}\n"
                    msg += f"🔗 `...{addr[-8:]}`\n"
                    msg += f"💰 *Toplam Bakiye:* `${total_usd:,.2f}`\n"
                    msg += f"📋 *Büyük Pozisyonlar (Büyük -> Küçük):*\n"
                    
                    if not sorted_tokens:
                        msg += " └ 🚫 _5,000$ üzeri büyük yatırım bulunmuyor._\n"
                    else:
                        for mint, info in sorted_tokens:
                            val = info['usd_value']
                            if val >= 100000:
                                icon = "💎"
                            elif val >= 50000:
                                icon = "🟢"
                            else:
                                icon = "🔹"
                                
                            msg += f" {icon} *{info['symbol']}:* {info['amount']:,.2f} (`${val:,.2f}`)\n"
                    msg += f"◣ ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ ◢\n"
                
                send_telegram_message(chat_id, msg)
                
    except Exception as e:
        print(f"Webhook İşleme Hatası: {e}")
        
    return "OK", 200

@app.route('/')
def home():
    return "Bot is tracking whales perfectly with Telegram Pinned Message Cloud DB!", 200

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
                
            for old_mint, old_amount in list(old_tokens.items()):
                if old_mint not in current_tokens or current_tokens[old_mint]["amount"] <= 0:
                    alert_msg = (
                        f"🚨 *BALİNA POZİSYONU SIFIRLADI! (TÜMÜNÜ SATTI)* 🔴\n"
                        f"👤 *Cüzdan:* {nickname}\n"
                        f"🪙 *Token:* Eski Varlık (Sıfırlandı)\n"
                        f"📈 *Satılan Miktar:* {old_amount:,.2f}\n"
                        f"📊 *Kalan Portföy Toplamı:* ${total_usd:,.2f}"
                    )
                    send_telegram_message(ADMIN_CHAT_ID, alert_msg)

            for mint, info in current_tokens.items():
                if mint not in old_tokens:
                    if info["usd_value"] >= MIN_USD_VALUE:
                        alert_msg = (
                            f"🚨 *YENİ BÜYÜK POZİSYON ANLIK ALINDI!* 🟢\n"
                            f"👤 *Cüzdan:* {nickname}\n"
                            f"🪙 *Token:* {info['symbol']}\n"
                            f"💰 *Satın Alınan Değer:* ${info['usd_value']:,.2f}\n"
                            f"📊 *Cüzdan Toplamı:* ${total_usd:,.2f}"
                        )
                        send_telegram_message(ADMIN_CHAT_ID, alert_msg)
                
                else:
                    old_amount = old_tokens[mint]
                    new_amount = info["amount"]
                    diff = new_amount - old_amount
                    
                    if old_amount > 0:
                        change_ratio = diff / old_amount
                        
                        if change_ratio >= 0.10:
                            if info["usd_value"] >= 1000:
                                tx_msg = (
                                    f"🐳 *BALİNA HAREKETİ! [🟢 BÜYÜK ALIM YAPTI]*\n"
                                    f"👤 *Cüzdan:* {nickname}\n"
                                    f"🪙 *Token:* {info['symbol']}\n"
                                    f"📈 *Miktar Artışı:* +{abs(diff):,.2f}\n"
                                    f"💰 *Mevcut Token Değeri:* ${info['usd_value']:,.2f}\n"
                                    f"📊 *Cüzdan Toplam Değeri:* ${total_usd:,.2f}"
                                )
                                send_telegram_message(ADMIN_CHAT_ID, tx_msg)
                        
                        elif change_ratio <= -0.30:
                            if info["usd_value"] >= 1000 or (old_amount * (info["usd_value"]/new_amount)) >= 1000:
                                tx_msg = (
                                    f"🐳 *BALİNA HAREKETİ! [🔴 EN AZ %30 SATIŞ YAPTI]*\n"
                                    f"👤 *Cüzdan:* {nickname}\n"
                                    f"🪙 *Token:* {info['symbol']}\n"
                                    f"📉 *Miktar Azalışı:* -{abs(diff):,.2f} (Adedin %{abs(change_ratio)*100:.0f}'ı)\n"
                                    f"💰 *Kalan Token Değeri:* ${info['usd_value']:,.2f}\n"
                                    f"📊 *Cüzdan Toplam Değeri:* ${total_usd:,.2f}"
                                )
                                send_telegram_message(ADMIN_CHAT_ID, tx_msg)

            tracked_wallets[address] = {mint: info["amount"] for mint, info in current_tokens.items()}
            save_data_to_telegram()
            time.sleep(2)
        time.sleep(60)

# --- BOT BAŞLANGIÇ TETİKLEYİCİSİ ---
load_data_from_telegram()

t_tracker = threading.Thread(target=tracker_loop, daemon=True)
t_tracker.start()
