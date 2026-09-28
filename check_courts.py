#!/usr/bin/env python3
"""Court watch: pings your phone (ntfy) when a 20:00-21:00 slot opens
at The Challenge Tennis Club. Runs on GitHub Actions every ~10 minutes."""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://court.ozzy.asia/court-demo"
TARGET = "20.00-21.00"      # slot label to watch (spaces ignored)
TARGET_START_HOUR = 20      # skip tonight's slot once it has started
COURTS = 6
STATE_FILE = "state.json"
FAIL_ALERT_AFTER = 6        # ~1 hour of failed checks before it warns you
BKK = timezone(timedelta(hours=7))
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def ntfy(title, message, priority="urgent", tags="tennis"):
    if not TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": URL},
    )
    urllib.request.urlopen(req, timeout=20).read()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f), False
    except FileNotFoundError:
        return {"alerted": [], "fails": 0, "fail_alerted": False}, True


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def fetch():
    # Same request the page makes in the background to draw the grid
    req = urllib.request.Request(
        URL,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (court-watch)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def find_open_slots(data, now):
    labels = data["timeLabels"]
    pairs = labels.items() if isinstance(labels, dict) else enumerate(labels)
    row_key = None
    for idx, label in pairs:
        if label and label.replace(" ", "").replace(":", ".") == TARGET:
            row_key = str(int(idx) + 1)  # the page looks bookings up at label index + 1
            break
    if row_key is None:
        raise ValueError("20:00-21:00 row not found - the site layout may have changed")

    today = now.strftime("%d/%m")
    slots = []
    for d in data["dateInfos"]:
        date_str = d["dateStr"]                       # e.g. "Tue 29/09"
        if date_str.endswith(today) and now.hour >= TARGET_START_HOUR:
            continue                                  # tonight's slot already started
        day = d.get("dayNum", d.get("DayNum"))
        bookings = data["bookingMap"].get(str(day), {})
        for court in range(1, COURTS + 1):
            if bookings.get(str(court), {}).get(row_key) is not True:
                slots.append(f"{date_str} - Court {court}")
    return slots


def main():
    state, first_run = load_state()
    now = datetime.now(BKK)
    state["heartbeat"] = now.strftime("%Y-%m-%d")  # daily commit keeps GitHub's schedule alive

    try:
        slots = find_open_slots(fetch(), now)
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        print(f"Check failed ({state['fails']} in a row): {e}")
        if state["fails"] >= FAIL_ALERT_AFTER and not state.get("fail_alerted"):
            try:
                ntfy("Court bot is stuck", f"Checks failing for ~1 hour.\nLast error: {e}",
                     priority="default", tags="warning")
                state["fail_alerted"] = True
            except Exception as ne:
                print(f"ntfy failed too: {ne}")
        save_state(state)
        return

    if state.get("fail_alerted"):
        try:
            ntfy("Court bot is back", "Checks are working again.", priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"ntfy failed: {ne}")
    state["fails"] = 0
    state["fail_alerted"] = False

    if first_run:
        try:
            ntfy("Court bot is live", "Watching 20:00-21:00, all 6 courts, next 15 days, every ~10 min.",
                 priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"ntfy failed: {ne}")

    already = set(state.get("alerted", []))
    new = [s for s in slots if s not in already]
    if new:
        try:
            ntfy(f"20:00-21:00 OPEN ({len(new)})", "\n".join(new) + "\n\nMessage the admin now.")
        except Exception as ne:
            print(f"ntfy failed, will retry next run: {ne}")
            slots = [s for s in slots if s not in new]

    print("Open now:", slots or "none", "| new:", new or "none")
    state["alerted"] = slots  # booked-again slots drop off, so a re-open pings you again
    save_state(state)


if __name__ == "__main__":
    main()
