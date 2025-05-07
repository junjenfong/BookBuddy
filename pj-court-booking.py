import requests
import schedule
import time
from datetime import datetime, timedelta
from collections import defaultdict
import re
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_URL = os.getenv("API_URL")

ACTIVITY_THREAD_IDS = {
    "Tennis": 32,
    "Badminton": 34
}

SPORT_TYPES = {
    "Tennis": [
        {"location": "Kelana Jaya", "ETID": 4, "FSITEID": 122, "FTYPEID": 163},
        {"location": "SS21", "ETID": 4, "FSITEID": 24, "FTYPEID": 33},
        {"location": "SS3", "ETID": 4, "FSITEID": 23, "FTYPEID": 171},
        {"location": "BU", "ETID": 4, "FSITEID": 22,"FTYPEID": 25},
        {"location": "Astaka", "ETID": 4, "FSITEID": 81,"FTYPEID": 122},
        {"location": "SS2", "ETID": 4, "FSITEID": 174,"FTYPEID": 257}
    ],
    "Badminton": [
        {"location": "SS21", "ETID": 1, "FSITEID": 24, "FTYPEID": 32},
        {"location": "BU11","ETID": 1,"FSITEID": 172,"FTYPEID": 256,},
        {"location": "BU","ETID": 1,"FSITEID": 22,"FTYPEID": 24,"FITEMID": 0,}
    ]
}

def send_telegram_message(text, thread_id):
    max_length = 4000
    for i in range(0, len(text), max_length):
        chunk = text[i:i + max_length]
        payload = {
            "chat_id": CHAT_ID,
            "message_thread_id": thread_id,
            "text": chunk,
            "parse_mode": "Markdown"
        }
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)

def fetch_slots(location, config, date):
    date_str = date.strftime("%Y-%m-%d")
    display_date_str = date.strftime("%d/%m/%Y")
    payload = {
        "ETID": config["ETID"],
        "FSITEID": config["FSITEID"],
        "FTYPEID": config["FTYPEID"],
        "FITEMID": 0,
        "CKIDATE": date_str,
        "CKODATE": date_str,
        "STARTTIME": "07:00",
        "ENDTIME": "23:00",
        "SEARCHMODE": "ONLINE"
    }
    try:
        response = requests.post(API_URL, json=payload, verify=False)
        slots = response.json()
        return [
            {
                "date": display_date_str,
                "location": location,
                "name": slot["NAME"],
                "name_ms": slot["NAMEMS"],
                "start": slot["STARTTIME"],
                "end": slot["ENDTIME"]
            }
            for slot in slots
            if slot.get("ISBOOKED") == 0 and int(slot.get("STARTTIME", "00:00")[:2]) >= 19
        ]
    except requests.exceptions.RequestException as e:
        print(f"❌ Error for {location}:", e)
        return []

def run_all_sports():
    today = datetime.today()
    days_to_check = 30

    for activity, courts in SPORT_TYPES.items():
        thread_id = ACTIVITY_THREAD_IDS.get(activity)
        start_date = today.strftime("%d/%m/%Y")
        end_date = (today + timedelta(days=days_to_check)).strftime("%d/%m/%Y")

        # Group by location -> date -> (start, end) -> list of courts
        grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        for i in range(days_to_check):
            date = today + timedelta(days=i)
            for court in courts:
                slots = fetch_slots(court["location"], court, date)
                for slot in slots:
                    key = (slot["start"], slot["end"])
                    grouped[slot["location"]][slot["date"]][key].append(slot["name"])

        msg = f"🎯 *{activity}* availability from ({start_date} - {end_date}):\n"

        if grouped:
            for location, dates in grouped.items():
                msg += f"\n📍 *{location}*"
                for date, timeslots in dates.items():
                    msg += f"\n\n📅 {date}"
                    for (start, end), courts in timeslots.items():
                        start_time = datetime.strptime(start, "%H:%M").strftime("%I:%M %p").lstrip("0")
                        end_time = datetime.strptime(end, "%H:%M").strftime("%I:%M %p").lstrip("0")
                        time_range = f"{start_time} - {end_time}"
                        courts_str = ", ".join(sorted({re.sub(r'.*?(Court\s*|Gelanggang\s*Badminton\s*)', '', c, flags=re.IGNORECASE) for c in courts}))
                        msg += f"\n• {time_range} → {courts_str}"
                 
                msg += "\n--------------------\n\n"
        else:
            msg += "No session found."

        msg += "Check now: https://mypjtempahan.mbpj.gov.my/"
        msg += "\n\nNext check running in next hour..."
        send_telegram_message(msg, thread_id)

# Run now
run_all_sports()

# Schedule hourly
schedule.every().day.at("00:00").do(run_all_sports)
schedule.every().day.at("06:00").do(run_all_sports)
schedule.every().day.at("12:00").do(run_all_sports)
schedule.every().day.at("18:00").do(run_all_sports)

print("🔁 Bot running. Press Ctrl+C to stop.")
while True:
    schedule.run_pending()
    time.sleep(1)
