import sys
sys.stdout.reconfigure(line_buffering=True)
import os
import time
import threading
from flask import Flask
import requests

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def home():
    return "Bot is active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

# Αποθήκευση προηγούμενων αποδόσεων για σύγκριση
previous_odds = {}

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def check_odds():
    global previous_odds
    if not ODDS_API_KEY:
        print("Missing ODDS_API_KEY in Environment Variables!")
        return
        
    print("Checking bet365 odds across all markets for changes...")
    # Ζητάει όλες τις αγορές (h2h, totals, spreads)
    url = f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={ODDS_API_KEY}&regions=eu&bookmakers=bet365&markets=h2h,totals,spreads"
    
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            events = res.json()
            print(f"Fetched {len(events)} soccer events.")
            
            alerts = []
            for match in events:
                match_id = match.get('id')
                home = match.get('home_team')
                away = match.get('away_team')
                
                bookmakers = match.get('bookmakers', [])
                if not bookmakers:
                    continue
                
                bm = bookmakers[0]
                current_match_odds = {}
                
                # Έλεγχος σε όλες τις κατηγορίες (h2h, totals, spreads)
                for market in bm.get('markets', []):
                    market_key = market.get('key')
                    for outcome in market.get('outcomes', []):
                        name = outcome.get('name')
                        point = outcome.get('point', '')
                        price = outcome.get('price')
                        
                        # Δημιουργία μοναδικού αναγνωριστικού για κάθε σημείο/αγορά
                        outcome_key = f"{market_key}_{name}_{point}"
                        current_match_odds[outcome_key] = {
                            'market': market_key,
                            'name': name,
                            'point': point,
                            'price': price
                        }
                
                # Σύγκριση με τις προηγούμενες τιμές
                if match_id in previous_odds:
                    old_odds = previous_odds[match_id]
                    for key, data in current_match_odds.items():
                        if key in old_odds:
                            old_price = old_odds[key]['price']
                            new_price = data['price']
                            
                            if old_price != new_price:
                                diff = round(new_price - old_price, 2)
                                direction = "📈 ΑΝΟΔΟΣ" if diff > 0 else "📉 ΠΤΩΣΗ"
                                point_info = f" ({data['point']})" if data['point'] else ""
                                
                                alerts.append(
                                    f"🚨 *ΑΛΛΑΓΗ ΑΠΟΔΟΣΗΣ!*\n"
                                    f"⚽ *{home} vs {away}*\n"
                                    f"📊 Αγορά: *{data['market'].upper()}*\n"
                                    f"🎯 Σημείο: *{data['name']}{point_info}*\n"
                                    f"💰 Από {old_price} ➡️ *{new_price}* ({direction} {abs(diff)})\n"
                                    f"🏦 Bookmaker: bet365"
                                )
                
                # Ενημέρωση της μνήμης
                previous_odds[match_id] = current_match_odds
            
            # Αποστολή ειδοποιήσεων στο Telegram
            if alerts:
                for alert in alerts[:5]:  # Αποστολή των αλλαγών
                    send_telegram_alert(alert)
                    time.sleep(1)
            else:
                print("No changes detected in odds.")
                
        else:
            print(f"API Error Status: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"Scan Exception: {e}")

def bot_loop():
    time.sleep(5)
    send_telegram_alert("🟢 *bet365 Odds Change Monitor Active!*")
    while True:
        try:
            check_odds()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(300) # Έλεγχος κάθε 5 λεπτά

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    run_flask()
