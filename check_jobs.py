import os
import re
import sys
import json
import time
import base64
import requests
from datetime import date, timedelta

STATE_FILE = "seen_jobs.json"
AUTH_STATUS_FILE = "auth_status.json"
DATES_FILE = "job_dates.json"
BOOKED_FILE = "booked_jobs.json"
VENUES_FILE = "booked_venues.json"
CLIENTS_FILE = "client_names.json"

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
TOTAL_RUNTIME_SECONDS = int(os.environ.get("TOTAL_RUNTIME_SECONDS", "270"))

API_URL = (
    "https://production-gateways.hgem.com/diner-gateway/guestservices-visitsapi/AvailableVisits"
    "?CurrentPosition.Latitude=53.263099670410156"
    "&CurrentPosition.Longitude=-2.9160826206207275"
    "&pageStart=0&pageSize=50&DateFilterType=0&FilterType=0&SortType=0"
)
BOOK_URL = "https://production-gateways.hgem.com/diner-gateway/guestservices-visitsapi/AvailableVisits/Book"

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# ---------- TOKEN REMINDERS ----------
TOKEN_WARN_HOURS = 6           # start warning this many hours before the token runs out
WARN_REPEAT_MINUTES = 60       # how often to repeat the early warning
EXPIRED_REPEAT_MINUTES = 30    # how often to repeat the "expired" alert
# -------------------------------------

# ---------- AUTO-BOOKING SETTINGS (edit these) ----------
AUTO_BOOK = os.environ.get("AUTO_BOOK", "off") == "on"
DRY_RUN = os.environ.get("DRY_RUN", "on") == "on"
MAX_BOOKINGS_PER_RUN = 1

# Normal jobs: vendors to book, and how far away they can be
WANTED_CLIENTS = [
    "Cosy Club",
    "Malmaison",
    "Hotel du Vin",
    "Loungers",
    "The Lounges",
    "Vapiano",
    "Country Village Inns",
    "Troia",
    "Botanist",
    "The Botanist",
    "New World Trading Company",
    "Cherry Tree",
    "The Cherry Tree",
    "Q Hotels",
    "Cut and Craft",
    "The Cut and Craft",
    "The Cut & Craft",
    "Cut & Craft",
    "Lake Vyrnwy Hotel & Spa",
    "Pranzo",
    "Pranzo Italian",
]
MAX_DISTANCE_KM = 70

# The only days normal (non-hotel) visits can be booked for. Put [] to allow any day.
NORMAL_DAYS = ["Friday", "Saturday", "Sunday"]

# Vendors allowed to be further away than the normal limit (in km)
VENDOR_MAX_KM = {
    "Longbow Venues": 120,
}

# Hotel jobs (any listing that says "overnight stay")
WANTED_HOTEL_CLIENTS = [
    "Malmaison",
    "Hotel du Vin",
    "Country Village Inns",
    "Coaching Inn Group",
    "The Coaching Inn Group",
    "Q Hotels",
    "Dakota Hotels",
    "Leonardo",
    "Hyll Hotel",
    "Andrew Brownsword Hotels",
    "Champneys Resorts",
    "Champneys",
    "Chapneys Hotels",
    "Firmdale Hotels",
    "Andrew Brownsword",
    "Guest House",
    "Guest House Hotels",
    "Cameron House",
    "Cameron House On Loch Lomond",
    "Lake Vyrnwy Hotel & Spa",
]
HOTEL_MAX_DISTANCE_KM = 500    # distance limit for hotels on your list above (0 = no limit)
HOTEL_DAYS = ["Friday", "Saturday"]

# ANY overnight stay, from ANY vendor, is also booked if it is within this many km
# (straight line, roughly a two hour drive). Put 0 to switch this off.
ANY_HOTEL_MAX_KM = 140

# Venues you never want booked automatically (part of the venue name is enough,
# copy it from the second line of an alert)
NEVER_BOOK_VENUES = []         # e.g. ["Congleton", "Bakewell"]

# Job numbers you never want booked
NEVER_BOOK_IDS = []            # e.g. [22886528, 22755799]

# After a venue is booked, don't book that venue again for this many days.
# This stops a cancelled visit being re-booked. Put 0 to switch off.
VENUE_COOLDOWN_DAYS = 28

# Applies to everything
MIN_DAYS_AHEAD = 11            # only book visits at least this many days away
MIN_REIMBURSEMENT = 0          # 0 means any fee
WANTED_VISIT_TYPES = []        # optional. "MV" = visit, "DA" = delivery assessment
# --------------------------------------------------------

bookings_made = 0


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_notification(title, message, tags="bell", priority="high"):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set, skipping notification:", title, message)
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except requests.RequestException as e:
        print("Could not send notification:", e)


def token_seconds_left():
    """How long the login token has left, if its expiry can be read. None if unknown."""
    try:
        payload = AUTH_TOKEN.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"]) - time.time()
    except Exception:
        return None


def check_token_expiry():
    """Warns you before the token runs out, so you can refresh it in time."""
    left = token_seconds_left()
    if left is None or left <= 0 or left > TOKEN_WARN_HOURS * 3600:
        return
    status = load_json(AUTH_STATUS_FILE, {})
    if time.time() - status.get("last_warn_ts", 0) < WARN_REPEAT_MINUTES * 60:
        return
    send_notification(
        "Mystery Dining token expires soon",
        f"Your login token runs out in about {left / 3600:.1f} hours. "
        "Update the AUTH_TOKEN secret before then or auto-booking will stop.",
        "warning",
    )
    status["last_warn_ts"] = time.time()
    save_json(AUTH_STATUS_FILE, status)


def get_fee(job):
    fee = job.get("maxDinerReimbursement")
    if fee is None:
        fee = (job.get("questionnaireReimbursement") or {}).get("maxReimbursement")
    try:
        return float(fee)
    except (TypeError, ValueError):
        return None


def is_hotel_job(job):
    """True if 'overnight stay' appears anywhere in the job listing."""
    text = json.dumps(job).lower()
    return re.search(r"overnight\W*stay", text) is not None


def on_list(job, names):
    """True if the job's client name matches anything in the given list."""
    client = (job.get("clientName") or "").lower()
    return any(w.lower() in client for w in names)


def venue_on_cooldown(job, venues):
    """True if this venue was booked recently and should be left alone."""
    if not VENUE_COOLDOWN_DAYS:
        return False
    name = job.get("visitName") or ""
    when = venues.get(name)
    if not name or not when:
        return False
    try:
        return (date.today() - date.fromisoformat(when)).days < VENUE_COOLDOWN_DAYS
    except ValueError:
        return False


def log_clients(jobs):
    """Keeps a running list of every client name seen, and which ones post hotel jobs."""
    names = load_json(CLIENTS_FILE, {})
    for job in jobs:
        name = job.get("clientName")
        if not name:
            continue
        hotel = is_hotel_job(job)
        entry = names.get(name)
        if entry is None:
            names[name] = {
                "hotel": hotel,
                "example_venue": job.get("visitName", ""),
                "first_seen": str(date.today()),
            }
        elif hotel and not entry.get("hotel"):
            entry["hotel"] = True
            entry["example_venue"] = job.get("visitName", "")
    save_json(CLIENTS_FILE, dict(sorted(names.items())))


def pick_date(job, allowed_days=None):
    """Earliest available date that is far enough away (and on an allowed day), or None."""
    earliest_allowed = date.today() + timedelta(days=MIN_DAYS_AHEAD)
    good = []
    for d in job.get("availableDates") or []:
        try:
            day = date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        if day < earliest_allowed:
            continue
        if allowed_days and day.strftime("%A") not in allowed_days:
            continue
        good.append(day)
    return min(good).isoformat() if good else None


def find_booking_date(job):
    """Returns the date to book if the job passes every rule, otherwise None."""
    client = (job.get("clientName") or "").lower()
    venue = (job.get("visitName") or "").lower()

    if job.get("visitId") in NEVER_BOOK_IDS:
        return None

    if any(b.lower() in venue for b in NEVER_BOOK_VENUES):
        return None

    if is_hotel_job(job):
        allowed_days = HOTEL_DAYS
        if on_list(job, WANTED_HOTEL_CLIENTS):
            max_km = HOTEL_MAX_DISTANCE_KM
        elif ANY_HOTEL_MAX_KM:
            max_km = ANY_HOTEL_MAX_KM
        else:
            return None
    else:
        allowed_days = NORMAL_DAYS or None
        max_km = MAX_DISTANCE_KM
        if not on_list(job, WANTED_CLIENTS):
            return None

    for name, km in VENDOR_MAX_KM.items():
        if name.lower() in client and max_km:
            max_km = max(max_km, km)

    if max_km:
        distance = job.get("distance")
        if distance is None or distance / 1000 > max_km:
            return None

    if MIN_REIMBURSEMENT > 0:
        fee = get_fee(job)
        if fee is None or fee < MIN_REIMBURSEMENT:
            return None

    if WANTED_VISIT_TYPES and job.get("visitType") not in WANTED_VISIT_TYPES:
        return None

    return pick_date(job, allowed_days)


def book_job(job, chosen_date):
    details = {
        "VisitId": job.get("visitId"),
        "Date": chosen_date,
        "IsRescheduling": False,
    }
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        resp = requests.post(BOOK_URL, headers=headers, json=details, timeout=15)
    except requests.RequestException as e:
        print("Booking request failed:", e)
        return False
    print("Booking response:", resp.status_code, resp.text[:200])
    return resp.ok


def check_once():
    global bookings_made
    seen_ids = set(load_json(STATE_FILE, []))
    booked_ids = set(load_json(BOOKED_FILE, []))
    venues = load_json(VENUES_FILE, {})
    old_counts = load_json(DATES_FILE, {})

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        resp = requests.get(API_URL, headers=headers, timeout=15)
    except requests.RequestException as e:
        print("Request failed:", e)
        return

    if resp.status_code in (401, 403):
        auth_status = load_json(AUTH_STATUS_FILE, {})
        if time.time() - auth_status.get("last_fail_ts", 0) >= EXPIRED_REPEAT_MINUTES * 60:
            send_notification(
                "Mystery Dining token EXPIRED",
                "Job checks and auto-booking have stopped. Get a new token and update the AUTH_TOKEN secret.",
                "rotating_light",
                "urgent",
            )
            auth_status["last_fail_ts"] = time.time()
            save_json(AUTH_STATUS_FILE, auth_status)
        else:
            print("Token expired, already reminded recently.")
        return

    if resp.status_code != 200:
        print("Platform returned an error, skipping this check:", resp.status_code)
        return

    try:
        jobs = resp.json()
    except ValueError:
        print("Could not read the job list, skipping this check.")
        return

    if not isinstance(jobs, list):
        print("Unexpected reply from the platform, skipping this check.")
        return

    log_clients(jobs)

    is_first_run = not os.path.exists(STATE_FILE)

    current_ids = set()
    new_jobs = []
    new_counts = {}
    unhidden = set()

    for job in jobs:
        vid = job.get("visitId")
        if vid is None:
            continue
        current_ids.add(vid)
        count = len(job.get("availableDates") or [])
        new_counts[str(vid)] = count
        if vid not in seen_ids and not is_first_run:
            new_jobs.append(job)
        elif old_counts.get(str(vid)) == 0 and count > 0 and not is_first_run:
            new_jobs.append(job)
            unhidden.add(vid)

    for job in new_jobs:
        fee = get_fee(job)
        symbol = job.get("currencySymbol", "£")
        fee_text = f"Up to {symbol}{fee:g}" if fee is not None else "Fee not shown"
        label = "Now available" if job.get("visitId") in unhidden else "New job"
        title = f"{label}: {job.get('clientName', 'Unknown')}"
        message = (
            f"{job.get('visitName', '')}\n"
            f"{job.get('address1', '')}, {job.get('postCode', '')}\n"
            f"{fee_text}"
        )
        if is_hotel_job(job):
            if on_list(job, WANTED_HOTEL_CLIENTS):
                message += "\nOvernight stay"
            else:
                message += "\nOvernight stay (vendor not on your hotel list)"

        # Book first, because speed matters. Tell you straight afterwards.
        booking_note = None
        if (AUTO_BOOK and bookings_made < MAX_BOOKINGS_PER_RUN
                and job.get("visitId") not in booked_ids
                and not venue_on_cooldown(job, venues)):
            chosen_date = find_booking_date(job)
            if chosen_date:
                weekday = date.fromisoformat(chosen_date).strftime("%A")
                kind = "Hotel overnight stay" if is_hotel_job(job) else "Visit"
                details = (
                    f"{title}\n{message}\n{kind} date: {weekday} {chosen_date}"
                    f"\nJob number: {job.get('visitId')}"
                )
                if DRY_RUN:
                    booking_note = ("Would have booked", details, "eyes")
                elif book_job(job, chosen_date):
                    bookings_made += 1
                    booked_ids.add(job.get("visitId"))
                    save_json(BOOKED_FILE, list(booked_ids))
                    if job.get("visitName"):
                        venues[job.get("visitName")] = str(date.today())
                        save_json(VENUES_FILE, venues)
                    booking_note = ("BOOKED", details, "white_check_mark")
                else:
                    booking_note = ("Booking FAILED", details, "warning")
        elif AUTO_BOOK and (job.get("visitId") in booked_ids or venue_on_cooldown(job, venues)):
            print("Skipping, already booked or on cooldown:", job.get("visitName"))

        send_notification(title, message)
        print("Notified:", title)
        if booking_note:
            send_notification(*booking_note)

    save_json(STATE_FILE, list(current_ids))
    save_json(DATES_FILE, new_counts)

    if is_first_run:
        print(f"Baseline set with {len(current_ids)} jobs, no alerts sent.")
    else:
        print(f"Checked {len(current_ids)} jobs, {len(new_jobs)} new.")


def run_test():
    today = date.today()
    earliest = today + timedelta(days=MIN_DAYS_AHEAD)

    def make_dates(days):
        return [(today + timedelta(days=i)).isoformat() + "T00:00:00" for i in days]

    every_day = make_dates(range(1, 60))
    monday_only = [d for d in every_day if date.fromisoformat(d[:10]).weekday() == 0]
    tuesday_only = [d for d in every_day if date.fromisoformat(d[:10]).weekday() == 1]
    too_soon = make_dates(range(1, MIN_DAYS_AHEAD))

    normal_vendor = WANTED_CLIENTS[0] if WANTED_CLIENTS else "No vendor set"
    hotel_vendor = WANTED_HOTEL_CLIENTS[0] if WANTED_HOTEL_CLIENTS else "No vendor set"
    stay = "Includes an overnight stay."

    def fake(vendor, km, dates, criteria="Visit for 2 adults."):
        return {
            "clientName": vendor,
            "visitId": 0,
            "visitType": "MV",
            "criteria": criteria,
            "distance": km * 1000,
            "maxDinerReimbursement": 100,
            "availableDates": dates,
        }

    near_ok = bool(ANY_HOTEL_MAX_KM and ANY_HOTEL_MAX_KM >= 100)
    tuesday_ok = not NORMAL_DAYS or "Tuesday" in NORMAL_DAYS

    cases = [
        ("Nearby normal job", fake(normal_vendor, 30, every_day), True),
        ("Normal job, Tuesdays only", fake(normal_vendor, 30, tuesday_only), tuesday_ok),
        ("Normal job too far away", fake(normal_vendor, 200, every_day), False),
        ("Normal job too soon", fake(normal_vendor, 30, too_soon), False),
        ("Hotel job, far away, any day", fake(hotel_vendor, 300, every_day, stay), True),
        ("Hotel job, Mondays only", fake(hotel_vendor, 300, monday_only, stay), False),
        ("Vendor not on your list", fake("Random Vendor", 30, every_day), False),
        ("Unlisted hotel, 100km away", fake("Random Hotel Co", 100, every_day, stay), near_ok),
        ("Unlisted hotel, 300km away", fake("Random Hotel Co", 300, every_day, stay), False),
    ]

    lines = []
    for name, job, should_book in cases:
        result = find_booking_date(job)
        ok = (result is not None) == should_book
        shown = "no booking"
        if result:
            day = date.fromisoformat(result)
            shown = f"{day.strftime('%A')} {result}"
            if day < earliest:
                ok = False
            allowed = HOTEL_DAYS if is_hotel_job(job) else NORMAL_DAYS
            if allowed and day.strftime("%A") not in allowed:
                ok = False
        lines.append(f"{'PASS' if ok else 'FAIL'}: {name} -> {shown}")

    for line in lines:
        print(line)
    send_notification("Booking rules test", "\n".join(lines), "test_tube")


def main():
    start = time.monotonic()
    run_count = 0

    try:
        check_token_expiry()
    except Exception as e:
        print("Could not check token expiry:", repr(e))

    while True:
        run_count += 1
        print(f"--- Check #{run_count} ---")
        try:
            check_once()
        except Exception as e:
            print("This check hit a problem, carrying on:", repr(e))

        elapsed = time.monotonic() - start
        remaining = TOTAL_RUNTIME_SECONDS - elapsed
        if remaining <= CHECK_INTERVAL_SECONDS:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)

    print(f"Finished {run_count} checks this run.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        main()
