import os
import time
import threading
from flask import Flask
import requests

# Web server για να μην βγάζει "No open ports" το Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DROP_THRESHOLD = 8.0

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram Error: {e}")

def check_dropping_odds():
    scraper_url = "https://api.statarea.com/v1/dropping-odds"
    print("Scanning ALL leagues for odds drops...")
    try:
        res = requests.get(scraper_url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for match in data.get("matches", []):
                drop = match.get("drop_percentage", 0)
                if drop >= DROP_THRESHOLD:
                    msg = (
                        f"⚠️ **ODDS DROP ALERT**\n\n"
                        f"🏆 League: {match.get('league')}\n"
                        f"⚽ Event: {match.get('home')} vs {match.get('away')}\n"
                        f"📉 Drop: {drop}%\n"
                        f"💰 Odds: {match.get('old_odds')} ➔ {match.get('new_odds')}"
                    )
                    send_telegram_alert(msg)
    except Exception as e:
        print(f"Scanning log: {e}")

def bot_loop():
    time.sleep(5)
    send_telegram_alert("🚀 *Odds Bot Online!*\nΠαρακολουθούνται όλες οι κατηγορίες 24/7.")
    while True:
        check_dropping_odds()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    run_flask()
