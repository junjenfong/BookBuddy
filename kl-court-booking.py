import os
import json
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page
import schedule
import time
import re
from dateutil import parser


# Load .env variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DBKL_USER = os.getenv("DBKL_USER")
DBKL_PASS = os.getenv("DBKL_PASS")

HASH_FILE = "dbkl_last_sent_hash.json"
SLOT_FILE = "dbkl_last_slots.json"
TARGET_URL_TEMPLATE = "https://tempahkl.dbkl.gov.my/facility/detail/book?location_id={}&start_date={}&sub_category=TENIS&toggle_step=1"
MAX_BOOKING_DAYS = 22
TIME_RE = re.compile(r"\d{1,2}:\d{2}\s[AP]M", re.IGNORECASE)

# Location configuration
LOCATIONS = {
    15: "Bukit Bandaraya",
    9: "Bangsar",
    7: "TTDI",
    10: "TITIWANGSA",
    11: "Bandar Tun Razak"
}

# Courts to ignore per location (court numbers to exclude)
# Format: {location_id: [court_numbers_to_ignore]}
IGNORED_COURTS = {
    10: [5, 6],
    # Example: 10: [1, 2],  # Ignore courts 1 and 2 for Titiwangsa
    # Add court numbers you want to ignore for each location
}



def parse_start_time(slot: str) -> datetime:
    match = TIME_RE.search(slot)
    if not match:
        raise ValueError(f"Invalid time slot format: {slot}")
    return datetime.strptime(match.group(), "%I:%M %p")

def escape_md_v2(text):
    return re.sub(r'([_*!\[\]()~`>#+=|{}\\\-.])', r'\\\1', text)


def send_telegram_message(text: str):
    import requests
    MAX_MESSAGE_LENGTH = 4000  # Leave some buffer below the 4096 limit
    
    # If message is short enough, send as single message
    if len(text) <= MAX_MESSAGE_LENGTH:
        payload = {
            "chat_id": CHAT_ID,
            "message_thread_id": 32,
            "text": text,
            "parse_mode": "MarkdownV2"
        }
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
        print(f"Telegram response: {response.status_code} - {response.text}")
        return
    
    # Split message into chunks
    lines = text.split('\n')
    current_chunk = ""
    chunk_count = 1
    
    for line in lines:
        # Check if adding this line would exceed the limit
        if len(current_chunk + line + '\n') > MAX_MESSAGE_LENGTH:
            # Send current chunk if it's not empty
            if current_chunk.strip():
                header = f"📱 *Part {chunk_count}*\n\n" if chunk_count > 1 else ""
                payload = {
                    "chat_id": CHAT_ID,
                    "text": header + current_chunk.strip(),
                    "parse_mode": "MarkdownV2"
                }
                response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
                print(f"Telegram response (part {chunk_count}): {response.status_code}")
                chunk_count += 1
                time.sleep(0.5)  # Small delay between messages
            
            # Start new chunk
            current_chunk = line + '\n'
        else:
            current_chunk += line + '\n'
    
    # Send remaining chunk
    if current_chunk.strip():
        header = f"📱 *Part {chunk_count}*\n\n" if chunk_count > 1 else ""
        payload = {
            "chat_id": CHAT_ID,
            "text": header + current_chunk.strip(),
            "parse_mode": "MarkdownV2"
        }
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
        print(f"Telegram response (part {chunk_count}): {response.status_code}")


def extract_court_number(label: str) -> int:
    """Extract court number from slot label
    Example: 'Court 1 06:00 PM to 07:00 PM' -> 1
    Example: 'Gelanggang 2 06:00 PM to 07:00 PM' -> 2
    """
    try:
        # Match both "Court" and "Gelanggang"
        match = re.search(r'(?:Court|Gelanggang)\s+(\d+)', label, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None
    except Exception:
        return None


def extract_time_range(label: str):
    """Extract start and end time strings from label
    Returns: (start_time_str, end_time_str) or (None, None)
    Handles: "07:00 PM - 09:00 PM" or "07:00 PM to 09:00 PM"
    """
    try:
        # Normalize spaces and separators
        clean_label = label.replace(" to ", "-").replace(" - ", "-")
        # Regex to find time range pattern: HH:MM AM/PM - HH:MM AM/PM
        match = re.search(r'(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)', clean_label, re.IGNORECASE)
        if match:
            return match.group(1), match.group(2)
        return None, None
    except Exception:
        return None, None


def is_slot_after_4pm(label: str) -> bool:
    """Check if slot starts between 7PM and 10PM (inclusive)"""
    try:
        start_time, _ = extract_time_range(label)
        if not start_time:
            return False
            
        dt = parser.parse(start_time)
        # 7PM onwards (19:00) but before 11PM (23:00)
        return 19 <= dt.hour < 23
    except Exception:
        return False


def should_ignore_court(location_id: int, court_number: int) -> bool:
    """Check if a court should be ignored based on configuration"""
    if location_id not in IGNORED_COURTS:
        return False
    return court_number in IGNORED_COURTS[location_id]


def fetch_slots_for_date_and_location(page: Page, location_id: int, date_str: str) -> list[str]:
    try:
        # Navigate to the target URL for the specific date and location
        url = TARGET_URL_TEMPLATE.format(location_id, date_str)
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Check if we were redirected to the login page (e.g., session expired)
        if "sign-in" in page.url:
            print("❌ Session expired. Re-login might be needed. Skipping this run.")
            return []

        # New approach: Find each court item and extract court number + slots
        all_slots = []
        court_items = page.locator("div.item.notranslate")
        
        for i in range(court_items.count()):
            court_item = court_items.nth(i)
            
            # Extract court number from the title (supports both COURT and GELANGGANG)
            title_text = court_item.locator("div.item-title").inner_text().strip()
            court_num_match = re.search(r'(?:COURT|GELANGGANG)\s+(\d+)', title_text, re.IGNORECASE)
            
            if not court_num_match:
                continue
            
            court_num = court_num_match.group(1)
            
            # Find all available slots (not taken) within this court
            available_slots = court_item.locator("div.slot:not(.taken)")
            
            for j in range(available_slots.count()):
                slot_label = available_slots.nth(j).locator("label").inner_text().strip()
                # Prepend court number to the slot label
                full_slot = f"Court {court_num} {slot_label}"
                all_slots.append(full_slot)
        
        return all_slots
    except Exception as e:
        print(f"An error occurred while fetching slots for location {location_id} on {date_str}: {e}")
        return []

def safe_goto(page: Page, url: str, wait_until="domcontentloaded", timeout=15000, retries=1):
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as e:
            print(f"❌ Attempt {attempt+1} failed to load {url}: {e}")
            if attempt < retries:
                print("🔄 Retrying...")
                time.sleep(3)
    return False


def process_and_notify(all_location_slots):
    """Process slot data and send notification if there are changes"""
    
    # Create a flattened set for comparison (including location info)
    if os.path.exists(SLOT_FILE):
        with open(SLOT_FILE, "r") as f:
            previous_slots = set(json.load(f))
    else:
        previous_slots = set()

    current_slots = set()
    for location_id, location_data in all_location_slots.items():
        for date, slots in location_data.items():
            for slot in slots:
                current_slots.add(f"{LOCATIONS[location_id]} - {date} - {slot}")

    new_slots = current_slots - previous_slots
    removed_slots = previous_slots - current_slots

    print(f"🆕 {len(new_slots)} new | 🗑️ {len(removed_slots)} removed")

    # Build the message with court grouping for better readability
    message_lines = []
    
    # Header with summary
    total_slots = sum(len(slots) for location_data in all_location_slots.values() for slots in location_data.values())
    header = f"🎾 *DBKL Tennis \\(7PM\\+\\)* \\- {total_slots} slots"
    if new_slots:
        header += f" \\| 🆕 {len(new_slots)} new"
    message_lines.append(header + "\n")
    
    for location_id, location_data in all_location_slots.items():
        location_name = LOCATIONS[location_id]
        message_lines.append(f"*{escape_md_v2(location_name)}*")
        
        for display_date, slots in sorted(location_data.items(), key=lambda item: datetime.strptime(item[0], "%d/%m/%Y")):
            # More compact date format
            date_obj = datetime.strptime(display_date, "%d/%m/%Y")
            short_date = date_obj.strftime("%d/%m")
            day_name = date_obj.strftime("%a")
            
            # Group slots by time slot (not by court)
            time_groups = {}
            for slot in slots:
                flat_key = f"{location_name} - {display_date} - {slot}"
                court_num = extract_court_number(slot)
                
                # Extract time only (remove court prefix)
                time_only = re.sub(r'(?:Court|Gelanggang)\s+\d+\s*', '', slot, flags=re.IGNORECASE).strip()
                
                is_new = flat_key in new_slots
                
                if time_only not in time_groups:
                    time_groups[time_only] = []
                time_groups[time_only].append((court_num, is_new))
            
            # Format date line
            message_lines.append(f"\n`{short_date} {day_name}`")
            
            # Format each time slot group
            # Sort by start time
            sorted_slots = []
            for time_slot in time_groups.keys():
                # Extract clean time for sorting
                # time_slot format is likely "7:00 PM - 9:00 PM" (from regex extraction) or original slot
                # But time_groups keys came from "time_only" extraction which preserves original separators? 
                # Actually earlier: time_only = re.sub(..., '', slot).strip() -> "7:00 PM - 9:00 PM"
                start, _ = extract_time_range(time_slot)
                if start:
                    dt = parser.parse(start)
                    sorted_slots.append((dt, time_slot))
            
            sorted_slots.sort(key=lambda x: x[0])
            
            for _, time_slot in sorted_slots:
                courts = time_groups[time_slot]
                court_strings = []
                for court_num, is_new in sorted(courts, key=lambda x: x[0] if x[0] is not None else 999):
                    if court_num is not None:
                        if is_new:
                            court_strings.append(f"{court_num}🆕")
                        else:
                            court_strings.append(str(court_num))
                
                courts_text = ", ".join(court_strings)
                
                # Format time nicely
                start_str, end_str = extract_time_range(time_slot)
                if start_str and end_str:
                    try:
                        start_fmt = parser.parse(start_str).strftime("%I:%M %p").lstrip("0")
                        end_fmt = parser.parse(end_str).strftime("%I:%M %p").lstrip("0")
                        message_lines.append(f"• {escape_md_v2(start_fmt)} \\- {escape_md_v2(end_fmt)} → {escape_md_v2(courts_text)}")
                    except:
                        # Fallback if parsing fails
                        message_lines.append(f"• {escape_md_v2(time_slot)} → {escape_md_v2(courts_text)}")
                else:
                    message_lines.append(f"• {escape_md_v2(time_slot)} → {escape_md_v2(courts_text)}")
        
        message_lines.append("")  # Space between locations
    
    # Only show removed slots if there are few of them
    if removed_slots and len(removed_slots) <= 5:
        message_lines.append("🗑️ *Removed:*")
        for slot in sorted(list(removed_slots)[:5]):  # Limit to 5 removed slots
            display_slot = slot.split(" - ", 2)
            if len(display_slot) == 3:
                # More compact removed format
                loc_short = display_slot[0][:4]  # Shorten location name
                date_short = display_slot[1].split("/")[0] + "/" + display_slot[1].split("/")[1]  # DD/MM only
                slot_short = display_slot[2].replace(" to ", "-")
                formatted_slot = f"{loc_short} {date_short} {slot_short}"
            else:
                formatted_slot = slot
            message_lines.append(f"~{escape_md_v2(formatted_slot)}~")
        message_lines.append("")
    
    message_lines.append("[🔗 Book](https://tempahkl.dbkl.gov.my)")
    message = "\n".join(message_lines)
    message_hash = hashlib.md5(message.encode()).hexdigest()

    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r") as f:
            last_hash = json.load(f)
    else:
        last_hash = {}

    if last_hash.get("hash") != message_hash:
        send_telegram_message(message)
        print("📤 Message sent to Telegram.")
        with open(HASH_FILE, "w") as f:
            json.dump({"hash": message_hash}, f, indent=2)
        with open(SLOT_FILE, "w") as f:
            json.dump(sorted(current_slots), f, indent=2)
    else:
        print("🔇 No changes in slot availability.")


def run():
    today = datetime.today()
    all_location_slots = {}

    try:
        with sync_playwright() as p:
            user_data_dir = "playwright_session"
            browser = p.chromium.launch_persistent_context(user_data_dir, headless=True)
            page = browser.new_page()

            print("Checking session and logging in if necessary...")

            if not safe_goto(page, "https://tempahkl.dbkl.gov.my/facility", retries=1):
                print("⚠️ Could not load facility page. Skipping this run.")
                browser.close()
                return

            if "sign-in" in page.url:
                print("🔑 Logging in...")
                if not safe_goto(page, "https://tempahkl.dbkl.gov.my/sign-in"):
                    print("⚠️ Login page not reachable. Skipping this run.")
                    browser.close()
                    return
                page.fill('input[name="email"]', DBKL_USER)
                page.fill('input[name="password"]', DBKL_PASS)
                page.click('button[type="submit"]')
                try:
                    page.wait_for_selector("a[href='/logout']", timeout=15000)
                    print("✅ Login successful.")
                except:
                    print("⚠️ Login failed or timed out.")
                    browser.close()
                    return

            for location_id, location_name in LOCATIONS.items():
                print(f"\n🏟️ Checking {location_name} (ID: {location_id})...")
                location_slots = {}
                
                # Apply -1 day offset for Titiwangsa only
                day_offset = -1 if location_id == 10 else 0
                
                for i in range(MAX_BOOKING_DAYS):
                    date = today + timedelta(days=i + day_offset)
                    date_str = date.strftime("%Y-%m-%d")
                    display_date = date.strftime("%d/%m/%Y")

                    print(f"  🔍 Checking {display_date}...")
                    url = TARGET_URL_TEMPLATE.format(location_id, date_str)
                    if not safe_goto(page, url):
                        continue  # Skip to next date

                    day_slots = fetch_slots_for_date_and_location(page, location_id, date_str)
                    
                    # Filter by time and court number
                    filtered = []
                    for slot in day_slots:
                        # Check time filter
                        if not is_slot_after_4pm(slot):
                            continue
                        
                        # Debug: Print raw slot format (first slot only to avoid spam)
                        if len(filtered) == 0 and day_slots:
                            print(f"    🔍 DEBUG - Raw slot format: '{slot}'")
                        
                        # Check court number filter
                        court_num = extract_court_number(slot)
                        if court_num is not None and should_ignore_court(location_id, court_num):
                            print(f"    ⏭️ Ignoring Court {court_num}: {slot}")
                            continue
                        
                        filtered.append(slot)

                    if filtered:
                        location_slots[display_date] = filtered

                if location_slots:
                    all_location_slots[location_id] = location_slots

            browser.close()

    except Exception as e:
        print(f"💥 Unexpected error during run: {e}")
        return

    if not all_location_slots:
        print("❌ No available slots found after 5 PM at any location.")
        return

    process_and_notify(all_location_slots)



if __name__ == "__main__":
    run()