import os
import time
import threading
from flask import Flask
import requests

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DROP_THRESHOLD = 8.0  # Ποσοστό πτώσης %
TARGET_BOOKMAKER = "bet365"  # Φίλτρο αποκλειστικά για bet365

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def check_dropping_odds():
    scraper_url = "https://api.statarea.com/v1/dropping-odds"
    print("Scanning bet365 across ALL markets (1X2, Over/Under, Asian) & leagues...")
    try:
        res = requests.get(scraper_url, timeout=15)
        if res.status_code == 200:
            data = res.json()
            for match in data.get("matches", []):
                bookmaker = str(match.get("bookmaker", "")).lower()
                
                # Φιλτράρισμα: Έλεγχος αν η απόδοση είναι από τη bet365
                if TARGET_BOOKMAKER.lower() in bookmaker or not bookmaker:
                    drop = match.get("drop_percentage", 0)
                    if drop >= DROP_THRESHOLD:
                        status = "🔴 LIVE" if match.get("is_live") else "📅 PRE-MATCH"
                        msg = (
                            f"⚠️ **BET365 ODDS DROP** ({status})\n\n"
                            f"🏆 **League:** {match.get('league')}\n"
                            f"⚽ **Event:** {match.get('home')} vs {match.get('away')}\n"
                            f"🎯 **Market:** {match.get('market_type', '1X2 / Totals')}\n"
                            f"📉 **Drop:** {drop}%\n"
                            f"💰 **Odds:** {match.get('old_odds')} ➔ {match.get('new_odds')}\n"
                            f"🏦 **Bookmaker:** bet365"
                        )
                        send_telegram_alert(msg)
    except Exception as e:
        print(f"Scan Log: {e}")

def bot_loop():
    time.sleep(5)
    send_telegram_alert("🟢 *bet365 Odds Bot Online!*\nΠαρακολουθούνται αποκλειστικά αποδόσεις bet365 (Pre-match & Live, όλες οι κατηγορίες & αγορές).")
    while True:
        check_dropping_odds()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    run_flask()
