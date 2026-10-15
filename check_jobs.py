import os
import re
import json
import time
import requests
from datetime import date, timedelta

STATE_FILE = "seen_jobs.json"
AUTH_STATUS_FILE = "auth_status.json"

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
    "Coaching Inn Group",
    "The Coaching Inn Group",
    "Longbow Venues",
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
    
                 ]            # e.g. ["Salad Project", "Loungers"]
MAX_DISTANCE_KM = 70

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
]      # e.g. ["Hotel Vendor One", "Hotel Vendor Two"]
HOTEL_MAX_DISTANCE_KM = 500    # 0 means no limit at all
HOTEL_DAYS = ["Friday", "Saturday"]

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


def send_notification(title, message, tags="bell"):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set, skipping notification:", title, message)
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": tags},
    )


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
    if is_hotel_job(job):
        wanted, max_km, allowed_days = WANTED_HOTEL_CLIENTS, HOTEL_MAX_DISTANCE_KM, HOTEL_DAYS
    else:
        wanted, max_km, allowed_days = WANTED_CLIENTS, MAX_DISTANCE_KM, None

    client = (job.get("clientName") or "").lower()
    if not wanted or not any(w.lower() in client for w in wanted):
        return None

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
    auth_status = load_json(AUTH_STATUS_FILE, {"last_fail_date": None})

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
        today = str(date.today())
        if auth_status.get("last_fail_date") != today:
            send_notification(
                "Mystery Dining token expired",
                "Your auth token needs refreshing. Grab a new one and update the AUTH_TOKEN secret.",
            )
            auth_status["last_fail_date"] = today
            save_json(AUTH_STATUS_FILE, auth_status)
        else:
            print("Already notified about expired token today.")
        return

    resp.raise_for_status()
    jobs = resp.json()

    is_first_run = not os.path.exists(STATE_FILE)

    current_ids = set()
    new_jobs = []

    for job in jobs:
        vid = job.get("visitId")
        if vid is None:
            continue
        current_ids.add(vid)
        if vid not in seen_ids and not is_first_run:
            new_jobs.append(job)

    for job in new_jobs:
        fee = get_fee(job)
        symbol = job.get("currencySymbol", "£")
        fee_text = f"Up to {symbol}{fee:g}" if fee is not None else "Fee not shown"
        title = f"New job: {job.get('clientName', 'Unknown')}"
        message = (
            f"{job.get('visitName', '')}\n"
            f"{job.get('address1', '')}, {job.get('postCode', '')}\n"
            f"{fee_text}"
        )
        send_notification(title, message)
        print("Notified:", title)

        if AUTO_BOOK and bookings_made < MAX_BOOKINGS_PER_RUN:
            chosen_date = find_booking_date(job)
            if chosen_date:
                weekday = date.fromisoformat(chosen_date).strftime("%A")
                kind = "Hotel overnight stay" if is_hotel_job(job) else "Visit"
                details = f"{title}\n{message}\n{kind} date: {weekday} {chosen_date}"
                if DRY_RUN:
                    send_notification("Would have booked", details, "eyes")
                elif book_job(job, chosen_date):
                    bookings_made += 1
                    send_notification("BOOKED", details, "white_check_mark")
                else:
                    send_notification("Booking FAILED", details, "warning")

    save_json(STATE_FILE, list(current_ids))

    if is_first_run:
        print(f"Baseline set with {len(current_ids)} jobs, no alerts sent.")
    else:
        print(f"Checked {len(current_ids)} jobs, {len(new_jobs)} new.")


def main():
    start = time.monotonic()
    run_count = 0

    while True:
        run_count += 1
        print(f"--- Check #{run_count} ---")
        check_once()

        elapsed = time.monotonic() - start
        remaining = TOTAL_RUNTIME_SECONDS - elapsed
        if remaining <= CHECK_INTERVAL_SECONDS:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)

    print(f"Finished {run_count} checks this run.")


if __name__ == "__main__":
    main()
