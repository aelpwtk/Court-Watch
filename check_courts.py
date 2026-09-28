#!/usr/bin/env python3
"""Court watch: pings your phone (ntfy) when you can play 20:00-22:00
at The Challenge Tennis Club: a free court at 20:00-21:00 AND a free court
at 21:00-22:00 on the same day (switching courts at 21:00 is fine).
Runs on GitHub Actions every ~10 minutes."""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://court.ozzy.asia/court-demo"
SLOTS = ["20.00-21.00", "21.00-22.00"]  # need a free court in EACH hour (spaces ignored)
START_HOUR = 20             # skip tonight once 20:00 has passed
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


def row_keys(data):
    labels = data["timeLabels"]
    pairs = labels.items() if isinstance(labels, dict) else enumerate(labels)
    # the page looks bookings up at label index + 1
    norm = {str(l).replace(" ", "").replace(":", "."): str(int(i) + 1) for i, l in pairs if l}
    missing = [s for s in SLOTS if s not in norm]
    if missing:
        raise ValueError(f"Rows {missing} not found - the site layout may have changed")
    return [norm[s] for s in SLOTS]


def courts_str(courts):
    return "/".join(str(c) for c in courts)


def find_hits(data, now):
    keys = row_keys(data)
    today = now.strftime("%d/%m")
    hits = {}
    for d in data["dateInfos"]:
        date_str = d["dateStr"]                       # e.g. "Tue 29/09"
        if date_str.endswith(today) and now.hour >= START_HOUR:
            continue                                  # tonight already started
        day = d.get("dayNum", d.get("DayNum"))
        bookings = data["bookingMap"].get(str(day), {})
        free = [[c for c in range(1, COURTS + 1)
                 if bookings.get(str(c), {}).get(k) is not True] for k in keys]
        if not all(free):
            continue
        same = sorted(set(free[0]) & set(free[1]))
        if same:
            hits[date_str] = f"{date_str} - Court {courts_str(same)}, both hours"
        else:
            hits[date_str] = (f"{date_str} - 20:00 Court {courts_str(free[0])}"
                              f" > 21:00 Court {courts_str(free[1])}")
    return hits


def main():
    state, first_run = load_state()
    now = datetime.now(BKK)
    state["heartbeat"] = now.strftime("%Y-%m-%d")  # daily commit keeps GitHub's schedule alive

    try:
        hits = find_hits(fetch(), now)
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
            ntfy("Court bot is live", "Watching 20:00-22:00 (court switch at 21:00 OK), next 15 days, every ~10 min.",
                 priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"ntfy failed: {ne}")

    already = set(state.get("alerted", []))
    new = [day for day in hits if day not in already]
    if new:
        try:
            ntfy(f"20:00-22:00 OPEN ({len(new)})",
                 "\n".join(hits[day] for day in new) + "\n\nMessage the admin now.")
        except Exception as ne:
            print(f"ntfy failed, will retry next run: {ne}")
            hits = {day: v for day, v in hits.items() if day not in new}

    print("Playable now:", list(hits.values()) or "none", "| new:", new or "none")
    state["alerted"] = list(hits)  # days that stop being playable drop off, so a re-open pings again
    save_state(state)

if __name__ == "__main__":
    main()
