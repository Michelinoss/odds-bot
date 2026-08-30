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
        
    print("Checking bet365 odds...")
    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"
    
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            events = res.json()
            print(f"Successfully fetched {len(events)} events!")
        else:
            print(f"API Error Status: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"Scan Exception: {e}")
def bot_loop():
    time.sleep(5)
    send_telegram_alert("🟢 *bet365 Odds Bot Online!*")
    while True:
        try:
            check_odds()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    run_flask()
