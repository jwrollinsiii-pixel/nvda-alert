"""
NVDA (NVIDIA) drop alert.

Checks the NVIDIA stock price and sends an alert to your iPhone
(through ntfy, Telegram and a real text message, if those are set up) when it
is down 1%, 2% or 3% from yesterday's close. Each level only alerts once per day.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------- Settings you can change ----------
ALERT_LEVELS = [1, 2, 3]    # alert when NVDA is down this many percent
MAX_DATA_AGE_MINUTES = 60   # ignore prices older than this (means market is closed)
STATE_FILE = "state.json"   # the agent's notebook of alerts already sent today
# ---------------------------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TEXTBELT_KEY = os.environ.get("TEXTBELT_KEY", "").strip()
# Keep only the digits of your phone number, and drop a leading US country code "1".
TEXTBELT_PHONE = "".join(ch for ch in os.environ.get("TEXTBELT_PHONE", "") if ch.isdigit())
if len(TEXTBELT_PHONE) == 11 and TEXTBELT_PHONE.startswith("1"):
    TEXTBELT_PHONE = TEXTBELT_PHONE[1:]
NEW_YORK = ZoneInfo("America/New_York")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/NVDA?range=5d&interval=1d"


def send_ntfy(title, message, tags="chart_with_downwards_trend"):
    """Send a push notification to your ntfy topic."""
    request = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": tags},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def send_telegram(title, message):
    """Send a message to your Telegram bot chat."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n{message}"}
        ).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def send_text(title, message):
    """Send a real text message (SMS) to your phone through Textbelt."""
    request = urllib.request.Request(
        "https://textbelt.com/text",
        data=urllib.parse.urlencode(
            {"phone": TEXTBELT_PHONE, "message": f"{title}: {message}", "key": TEXTBELT_KEY}
        ).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    # Textbelt answers "200 OK" even when it refuses, so check the answer itself.
    if not result.get("success"):
        raise RuntimeError(f"Textbelt refused the text: {result.get('error', 'unknown reason')}")
    print(f"Text sent. Texts left on your Textbelt key: {result.get('quotaRemaining')}")


def notify(title, message, ntfy_tags="chart_with_downwards_trend"):
    """Send through every channel that is set up.

    If one channel fails, the others still go out and the problem shows up as
    a warning in the GitHub log. The run only fails if NO channel worked.
    """
    problems = []
    sent_any = False

    if NTFY_TOPIC:
        try:
            send_ntfy(title, message, tags=ntfy_tags)
            sent_any = True
        except Exception as error:
            problems.append(f"ntfy failed: {error}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            send_telegram(title, message)
            sent_any = True
        except Exception as error:
            problems.append(f"Telegram failed: {error}")

    if TEXTBELT_KEY and TEXTBELT_PHONE:
        try:
            send_text(title, message)
            sent_any = True
        except Exception as error:
            problems.append(f"Text message failed: {error}")

    for problem in problems:
        print(f"::warning::{problem}")
    if not sent_any:
        raise RuntimeError("No alert channel worked: " + "; ".join(problems))


def get_nvda():
    """Return (current price, previous close, time of last trade)."""
    request = urllib.request.Request(YAHOO_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)

    result = data["chart"]["result"][0]
    current = result["meta"]["regularMarketPrice"]
    last_trade = datetime.fromtimestamp(result["meta"]["regularMarketTime"], tz=timezone.utc)
    market_day = last_trade.astimezone(NEW_YORK).date()

    # Previous close = the last daily close from a day before the latest trading day.
    previous_close = None
    closes = result["indicators"]["quote"][0]["close"]
    for timestamp, close in zip(result["timestamp"], closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(NEW_YORK).date()
        if day < market_day:
            previous_close = close

    if previous_close is None:
        raise RuntimeError("Could not find the previous close in the data.")
    return current, previous_close, last_trade


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    telegram_ready = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    text_ready = bool(TEXTBELT_KEY and TEXTBELT_PHONE)
    if not NTFY_TOPIC and not telegram_ready and not text_ready:
        sys.exit("No alert channel is set up. Add the secrets for at least one channel.")

    # Test mode: just prove the phone alerts work, then stop.
    if os.environ.get("SEND_TEST", "").lower() == "true":
        notify("NVDA alert test", "Success! Your NVDA alert agent can reach your phone.",
               ntfy_tags="white_check_mark")
        print("Test alert sent.")
        return

    current, previous_close, last_trade = get_nvda()
    change_pct = (current - previous_close) / previous_close * 100
    age_minutes = (datetime.now(timezone.utc) - last_trade).total_seconds() / 60
    print(f"NVDA: ${current:,.2f} | previous close: ${previous_close:,.2f} | "
          f"change: {change_pct:+.2f}% | last trade {age_minutes:.0f} min ago")

    if age_minutes > MAX_DATA_AGE_MINUTES:
        print("The price looks old, so the market is probably closed. Nothing to do.")
        return

    drop_pct = -change_pct
    levels_hit = [level for level in ALERT_LEVELS if drop_pct >= level]
    if not levels_hit:
        print("No alert level reached.")
        return

    today = datetime.now(NEW_YORK).date().isoformat()
    state = load_state()
    if state.get("date") != today:
        state = {"date": today, "last_level": 0}

    level = max(levels_hit)
    if level <= state["last_level"]:
        print(f"Already alerted for the {level}% level today.")
        return

    notify(
        f"NVDA down {level}%",
        f"NVDA is down {drop_pct:.2f}% today, at ${current:,.2f} "
        f"(yesterday's close: ${previous_close:,.2f}).",
    )
    state["last_level"] = level
    save_state(state)  # only saved after at least one alert was sent successfully
    print(f"Alert sent for the {level}% level.")


if __name__ == "__main__":
    main()
