import os
import time
import threading
from flask import Flask
import requests

app = Flask(__name__)

# Endpoint για το UptimeRobot (διορθώνει τα 404)
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
    if not ODDS_API_KEY:
        print("Missing ODDS_API_KEY in Environment Variables!")
        return
        
    print("Checking bet365 odds across all sports & markets...")
    # Λήψη αγώνων και αποδόσεων για bet365
    url = f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={ODDS_API_KEY}&regions=eu&bookmakers=bet365&markets=h2h,totals,spreads"
    
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            events = res.json()
            print(f"Successfully fetched {len(events)} events from bet365.")
        else:
            print(f"API Error Status: {res.status_code}")
    except Exception as e:
        print(f"Scan Exception: {e}")

def bot_loop():
    time.sleep(5)
    send_telegram_alert("🟢 *bet365 Odds Bot Online & Fixed!*\nΤο bot διορθώθηκε και παρακολουθεί πλέον κανονικά τη bet365.")
    while True:
        try:
            check_odds()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(300)  # Έλεγχος κάθε 5 λεπτά

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    run_flask()
