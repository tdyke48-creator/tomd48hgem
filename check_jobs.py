import os
import json
import time
import requests
from datetime import date

STATE_FILE = "seen_jobs.json"
AUTH_STATUS_FILE = "auth_status.json"

# How long to keep looping within a single scheduled run, and how long to
# wait between checks. Scheduled runs fire every 5 minutes, so this loop
# covers that whole window in ~60 second steps, leaving a safety margin
# before the next scheduled run starts.
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
TOTAL_RUNTIME_SECONDS = int(os.environ.get("TOTAL_RUNTIME_SECONDS", "270"))

API_URL = (
    "https://production-gateways.hgem.com/diner-gateway/guestservices-visitsapi/AvailableVisits"
    "?CurrentPosition.Latitude=53.263099670410156"
    "&CurrentPosition.Longitude=-2.9160826206207275"
    "&pageStart=0&pageSize=50&DateFilterType=0&FilterType=0&SortType=0"
)

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_notification(title, message):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set, skipping notification:", title, message)
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "bell"},
    )


def check_once():
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

    # First run just sets the baseline, no alerts for jobs already on the board
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
        title = f"New job: {job.get('clientName', 'Unknown')}"
        message = (
            f"{job.get('visitName', '')}\n"
            f"{job.get('address1', '')}, {job.get('postCode', '')}\n"
            f"Up to £{job.get('maxDinerReimbursement', '?')}"
        )
        send_notification(title, message)
        print("Notified:", title)

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
