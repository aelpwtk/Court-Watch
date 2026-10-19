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
FAIL_ALERT_AFTER = 135      # ~45 minutes of failed checks (3 checks/min) before it warns you
BKK = timezone(timedelta(hours=7))
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SHARED_TOPIC = os.environ.get("NTFY_TOPIC_SHARED", "").strip()   # court alerts only, for people Marc shares them with
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def _publish(topic, title, message, priority, tags):
    # JSON publish: HTTP headers can't carry emoji or dashes, so a header-based title silently failed
    body = {"topic": topic, "title": title, "message": message,
            "priority": PRIORITIES.get(priority, 3),
            "tags": [t for t in tags.split(",") if t], "click": URL}
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def ntfy(title, message, priority="urgent", tags="", share=False):
    """share=True is for real court alerts only: they also go to the shared topic (health pings never do)."""
    if not TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return
    _publish(TOPIC, title, message, priority, tags)       # a failure here raises, so the caller retries
    if share and SHARED_TOPIC:
        try:
            _publish(SHARED_TOPIC, title, message, priority, tags)
        except Exception as e:                            # best effort: never cause a duplicate alert for Marc
            print(f"shared ntfy failed: {e}")


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


TH_DAYS = {"Mon": "จ.", "Tue": "อ.", "Wed": "พ.", "Thu": "พฤ.",
           "Fri": "ศ.", "Sat": "ส.", "Sun": "อา."}


def th_date(date_str):
    """'Tue 29/09' -> 'อ. 29/09' (state keeps the site's own English text; only messages are Thai)."""
    head, _, rest = str(date_str).partition(" ")
    return f"{TH_DAYS.get(head, head)} {rest}".strip()


def courts_str(courts):
    return "/".join(str(c) for c in courts)


def find_hits(data, now):
    keys = row_keys(data)
    today = now.strftime("%d/%m")
    hits = {}
    all_dates = []                                    # every day the window shows, in order
    for d in data["dateInfos"]:
        date_str = d["dateStr"]                       # e.g. "Tue 29/09"
        all_dates.append(date_str)
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
            hits[date_str] = f"{th_date(date_str)} \u00b7 คอร์ท {courts_str(same)} (ทั้ง 2 ชม.)"
        else:
            hits[date_str] = (f"{th_date(date_str)} \u00b7 20:00 คอร์ท {courts_str(free[0])}"
                              f" \u2192 21:00 คอร์ท {courts_str(free[1])}")
    return hits, all_dates


def main():
    state, first_run = load_state()
    now = datetime.now(BKK)
    state["heartbeat"] = now.strftime("%Y-%m-%d")  # daily commit keeps GitHub's schedule alive

    try:
        hits, all_dates = find_hits(fetch(), now)
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        print(f"Check failed ({state['fails']} in a row): {e}")
        if state["fails"] >= FAIL_ALERT_AFTER and not state.get("fail_alerted"):
            try:
                ntfy("\U0001f535 Challenge \u2014 bot stuck", f"No data for ~1 hour.\n{e}",
                     priority="default", tags="warning")
                state["fail_alerted"] = True
            except Exception as ne:
                print(f"ntfy failed too: {ne}")
        save_state(state)
        return

    if state.get("fail_alerted"):
        try:
            ntfy("\U0001f535 Challenge \u2014 back online", "Checks working again.", priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"ntfy failed: {ne}")
    state["fails"] = 0
    state["fail_alerted"] = False

    if first_run:
        try:
            ntfy("\U0001f535 Challenge \u2014 now watching", "20:00-22:00 (court switch at 21:00 OK), next 15 days, every ~10 min.",
                 priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"ntfy failed: {ne}")

    # --- fresh-day radar: days that just rolled into the 15-day window since last run ---
    known = set(state.get("known_days", []))
    newly_released = [] if not known else [d for d in all_dates if d not in known]
    state["known_days"] = all_dates

    already = set(state.get("alerted", []))
    ordered = list(hits)                       # dateInfos order = chronological

    # a freshly-released day that's already a 20:00-22:00 block = first-dibs chance
    fresh = [d for d in ordered if d in newly_released and d not in already]
    if fresh:
        title = ("\U0001f6a8\U0001f195 วันใหม่เปิดจองแล้วจ้าวไอต้าว"
                 if len(fresh) == 1 else
                 f"\U0001f6a8\U0001f195 วันใหม่เปิดจอง {len(fresh)} วันแล้วจ้าวไอต้าว")
        try:
            ntfy(title, "\n".join(hits[d] for d in fresh)
                 + "\n\nเพิ่งเปิดจอง \u2014 รีบจองก่อนคนอื่น! กดแล้วทักแอดมิน", share=True)
            already |= set(fresh)              # don't double-ping in the normal block below
        except Exception as ne:
            print(f"ntfy failed (fresh), will retry next run: {ne}")

    new = [day for day in ordered if day not in already]
    if new:
        lines = [hits[day] + ("  \u2190 ใหม่" if day in new else "") for day in ordered]
        title = ("\U0001f6a8\U0001f3be คอร์ทว่างแล้วจ้าวไอต้าว"
                 if len(ordered) == 1 else
                 f"\U0001f6a8\U0001f3be คอร์ทว่าง {len(ordered)} วันแล้วจ้าวไอต้าว (ใหม่ {len(new)})")
        try:
            ntfy(title, "\n".join(lines) + "\n\nใครมาก่อนได้ก่อน \u2014 กดแล้วทักแอดมิน", share=True)
        except Exception as ne:
            print(f"ntfy failed, will retry next run: {ne}")
            hits = {day: v for day, v in hits.items() if day not in new}

    print("Playable now:", list(hits.values()) or "none", "| new:", new or "none", "| fresh:", fresh or "none")
    state["alerted"] = list(hits)  # days that stop being playable drop off, so a re-open pings again
    save_state(state)

if __name__ == "__main__":
    main()
