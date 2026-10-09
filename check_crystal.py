#!/usr/bin/env python3
"""Crystal Sports watch: pings your phone (ntfy) when a court opens up at Crystal Sports
(LOC001) or Crystal Sports G (LOC002), alert-only - it never books anything.
  Mon-Fri: 20:00-22:00  -> a free court at 20:00 AND a free court at 21:00 (switching courts is fine)
  Sat-Sun: 09:00-22:00  -> any free 1-hour slot counts; 2 hours in a row is flagged
The availability endpoint is public (no login). reservestatus "0" = free, "1" = taken.
Part of the loop in watch_loop.py. Fast lane (default): every round (~20 s) re-checks all Sat/Sun
days plus the newest day, and a rotating slice of the weekdays every 3rd round. The moment the site
answers RATE_LIMITED it backs off (65 s, then 5 min, then 15 min) and drops to the old gentle pace
(one round a minute, 5 days + newest in rotation) for 30 minutes."""

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://crystalsports-booking.kegroup.co.th"
API = BASE + "/api_helper.php?action=getAvailableStadiums"
BOOKING_URL = BASE + "/booking.php"
VENUES = {"LOC001": "Crystal Sports", "LOC002": "Crystal Sports G"}
WINDOW_DAYS = 14                 # booking page shows today + 13 days
WEEKDAY_HOURS = (20, 21)         # both hours needed on Mon-Fri
WEEKEND_HOURS = range(9, 22)     # slot starts 09:00 ... 21:00 on Sat-Sun
FREE = "0"
CHUNK = int(os.environ.get("CRYSTAL_CHUNK", 5))        # non-newest days checked per call
GAP = float(os.environ.get("CRYSTAL_GAP", 0.7))        # seconds between requests
BUDGET = float(os.environ.get("CRYSTAL_BUDGET", 30))   # max seconds one round may spend
FAST = os.environ.get("CRYSTAL_FAST", "1") != "0"      # fast lane on/off (kill switch: CRYSTAL_FAST=0)
WD_CHUNK = 3                     # fast lane: weekdays re-checked per rotation round
SAFE_MINUTES = 30                # after a rate limit, stay on the gentle pace this long
BACKOFFS = (65, 300, 900)        # quiet seconds after the 1st / 2nd / 3rd+ rate limit within 20 min
STATE_FILE = "state_crystal.json"
FAIL_ALERT_AFTER = 45            # ~45 minutes of failed checks before it warns you
BKK = timezone(timedelta(hours=7))
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_tick = 0                # which rotation slice we are on (per run)
_backoff_until = 0.0     # monotonic time before which we stay quiet after RATE_LIMITED
_safe_until = 0.0        # monotonic time until which we stay on the gentle pace
_rl_times = []           # monotonic times of recent rate limits
_round = 0               # how many times main() ran this run


class RateLimited(Exception):
    pass


# ---------- notifications & state ----------

def ntfy(title, message, priority="urgent", tags=""):
    if not TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return
    # JSON publish: HTTP headers can't carry emoji or dashes, so a header-based title silently failed
    body = {"topic": TOPIC, "title": title, "message": message,
            "priority": PRIORITIES.get(priority, 3),
            "tags": [t for t in tags.split(",") if t], "click": BOOKING_URL}
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def safe_ntfy(*args, **kwargs):
    try:
        ntfy(*args, **kwargs)
        return True
    except Exception as e:
        print(f"ntfy failed: {e}")
        return False


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


# ---------- website ----------

def fetch(date, loc):
    """One request = every court and hour for one venue on one day."""
    req = urllib.request.Request(
        API,
        data=json.dumps({"date": date, "locId": loc}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Origin": BASE, "Referer": BOOKING_URL},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("utf-8", "replace")
    if "RATE_LIMITED" in text:
        raise RateLimited(text[:120])
    try:
        rows = json.loads(text)
    except ValueError:
        raise RuntimeError(f"Unreadable reply: {text[:120]!r}")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"No court data returned for {loc} {date}: {text[:120]!r}")
    return rows


def free_by_hour(rows):
    """{hour: [court names]} for slots whose reservestatus is free."""
    out = {}
    for row in rows:
        if str(row.get("reservestatus")) != FREE:
            continue
        try:
            hour = int(str(row["timeStart"])[:2])
        except (KeyError, ValueError):
            continue
        out.setdefault(hour, []).append(str(row.get("stadiumName", "?")))
    return out


def pick_dates(dates, fast=False):
    """Newest day every call (that's where fresh releases appear) + more days.
    Fast lane: every Sat/Sun day each call (single freed slots matter there) and a rotating slice
    of the weekdays every 3rd call (a weekday needs both 20:00 and 21:00, so it's rarer).
    Gentle pace: just a rotating slice of the rest."""
    global _tick
    newest, others = dates[-1], dates[:-1]
    chosen = []
    if fast:
        weekend = [d for d in others if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5]
        weekdays = [d for d in others if d not in weekend]
        chosen = list(weekend)
        if weekdays and _tick % 3 == 0:
            start = ((_tick // 3) * WD_CHUNK) % len(weekdays)
            chosen += [weekdays[(start + i) % len(weekdays)] for i in range(min(WD_CHUNK, len(weekdays)))]
    elif others:
        start = (_tick * CHUNK) % len(others)
        chosen = [others[(start + i) % len(others)] for i in range(min(CHUNK, len(others)))]
    _tick += 1
    return sorted(set(chosen + [newest]))


def find_hits(day_free, now):
    """day_free: {date: {hour: [courts]}} for the days we just checked.
    Returns {key: info}: key is 'YYYY-MM-DD' (weekday, both hours) or 'YYYY-MM-DD HH' (weekend slot)."""
    hits = {}
    today = now.strftime("%Y-%m-%d")
    for ds, hours in day_free.items():
        weekday = datetime.strptime(ds, "%Y-%m-%d").weekday()
        if weekday < 5:
            if ds == today and now.hour >= WEEKDAY_HOURS[0]:
                continue                                   # tonight already started
            a, b = (sorted(hours.get(h, [])) for h in WEEKDAY_HOURS)
            if a and b:
                hits[ds] = {"kind": "day", "a": a, "b": b}
        else:
            for h in WEEKEND_HOURS:
                if ds == today and h <= now.hour:
                    continue                               # already started
                if hours.get(h):
                    hits[f"{ds} {h:02d}"] = {"kind": "hour", "courts": sorted(hours[h])}
    return hits


# ---------- message ----------

def nice_date(ds):
    return datetime.strptime(ds[:10], "%Y-%m-%d").strftime("%a %d/%m")


def courts_str(courts, limit=5):
    courts = list(courts)
    shown = ", ".join(courts[:limit])
    return shown + (f" +{len(courts) - limit}" if len(courts) > limit else "")


def day_line(ds, info):
    same = sorted(set(info["a"]) & set(info["b"]))
    if same:
        return f"{nice_date(ds)} · {courts_str(same)} (both hours)"
    return f"{nice_date(ds)} · 20h {courts_str(info['a'])} → 21h {courts_str(info['b'])}"


def two_hour_runs(hits, new_keys):
    runs = []
    for key in sorted(k for k in hits if hits[k]["kind"] == "hour"):
        ds, h = key[:10], int(key[11:])
        nxt = f"{ds} {h + 1:02d}"
        if nxt in hits and (key in new_keys or nxt in new_keys):
            a, b = hits[key]["courts"], hits[nxt]["courts"]
            same = sorted(set(a) & set(b))
            how = f"{courts_str(same)} both hours" if same else f"{courts_str(a)} > {courts_str(b)}"
            runs.append(f"{nice_date(ds)} {h:02d}:00-{h + 2:02d}:00 - {how}")
    return runs


def build_message(hits, new_keys, fresh=False):
    new_keys = sorted(new_keys)
    lines, by_day = [], {}
    for k in new_keys:
        if hits[k]["kind"] == "day":
            lines.append(day_line(k, hits[k]))
        else:
            by_day.setdefault(k[:10], []).append(f"{k[11:]}:00 {courts_str(hits[k]['courts'], 3)}")
    lines += [f"{nice_date(ds)} · " + ", ".join(slots) for ds, slots in by_day.items()]
    lines.sort()

    runs = two_hour_runs(hits, set(new_keys))
    if runs:
        lines += ["", "2 HOURS STRAIGHT:"] + runs
    extra = len(hits) - len(new_keys)
    if extra > 0:
        lines += ["", f"(+{extra} more still open in your windows)"]

    if fresh:
        days = sorted(set(k[:10] for k in new_keys))
        title = ("\U0001f48e Crystal — \U0001f195 new day open" if len(days) == 1
                 else f"\U0001f48e Crystal — \U0001f195 {len(days)} new days open")
        lines += ["", "Just released — grab it before others. Tap to open."]
    else:
        title = ("\U0001f48e Crystal — 2 hrs straight!" if runs
                 else f"\U0001f48e Crystal — court open ({len(new_keys)})")
        lines += ["", "Book it now — tap to open."]
    return title, "\n".join(lines)


# ---------- main ----------

def main():
    global _backoff_until, _safe_until, _round
    if time.monotonic() < _backoff_until:
        print("Crystal: cooling down after the site's rate limit - skipping this round.")
        return
    _round += 1
    fast = FAST and time.monotonic() >= _safe_until
    if not fast and (_round - 1) % 3 != 0:
        return                      # gentle pace: only one round in three (~1 a minute)
    state, first_run = load_state()
    now = datetime.now(BKK)
    dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(WINDOW_DAYS)]

    day_free, rate_limited, last_err, first_req = {}, False, None, True
    deadline = time.monotonic() + BUDGET
    for ds in pick_dates(dates, fast):
        if time.monotonic() > deadline:      # slow server: leave the rest for the next round
            break
        merged, ok = {}, True
        for loc in VENUES:
            if not first_req:
                time.sleep(GAP)
            first_req = False
            try:
                for hour, courts in free_by_hour(fetch(ds, loc)).items():
                    merged.setdefault(hour, []).extend(courts)
            except RateLimited:
                rate_limited, ok = True, False
                break
            except Exception as e:
                last_err, ok = e, False
                break
        if rate_limited:
            break
        if ok:                      # only trust a day when BOTH venues answered
            day_free[ds] = merged

    if rate_limited:
        now_m = time.monotonic()
        _rl_times[:] = [t for t in _rl_times if now_m - t < 1200] + [now_m]
        wait = BACKOFFS[min(len(_rl_times), len(BACKOFFS)) - 1]
        _backoff_until = now_m + wait
        _safe_until = now_m + SAFE_MINUTES * 60
        print(f"Crystal: RATE_LIMITED ({len(_rl_times)}x in 20 min) - backing off {wait}s, "
              f"gentle pace for {SAFE_MINUTES} min (not counted as a failure).")
        if len(_rl_times) >= 2:
            safe_ntfy("\U0001f48e Crystal \u2014 site pushed back",
                      f"Rate limit hit {len(_rl_times)}x in 20 min. Backed off and slowed down for {SAFE_MINUTES} min.",
                      priority="default", tags="warning")
    if not day_free:
        if last_err is not None:
            state["fails"] = state.get("fails", 0) + 1
            print(f"Crystal check failed ({state['fails']} in a row): {last_err}")
            if state["fails"] >= FAIL_ALERT_AFTER and not state.get("fail_alerted"):
                if safe_ntfy("\U0001f48e Crystal — bot stuck", f"No data for ~1 hour.\n{last_err}",
                             priority="default", tags="warning"):
                    state["fail_alerted"] = True
            save_state(state)
        return

    if state.get("fail_alerted"):
        safe_ntfy("\U0001f48e Crystal — back online", "Checks working again.",
                  priority="default", tags="white_check_mark")
    state["fails"] = 0
    state["fail_alerted"] = False
    if first_run:
        safe_ntfy("\U0001f48e Crystal — now watching",
                  "Mon-Fri 20:00-22:00 and Sat-Sun 09:00-22:00, both Crystal venues, next 14 days.",
                  priority="default", tags="white_check_mark")

    # --- fresh-day radar: a day that just rolled into the 14-day window ---
    known = set(state.get("known_days", []))
    newly_released = set() if not known else {d for d in dates if d not in known}
    state["known_days"] = dates

    hits = find_hits(day_free, now)
    already = set(state.get("alerted", []))

    fresh = [k for k in sorted(hits) if k[:10] in newly_released and k not in already]
    if fresh:
        title, body = build_message(hits, fresh, fresh=True)
        if safe_ntfy(title, body):
            already |= set(fresh)

    new = [k for k in sorted(hits) if k not in already]
    if new:
        title, body = build_message(hits, new)
        if not safe_ntfy(title, body):
            hits = {k: v for k, v in hits.items() if k not in new}   # retry next time

    # keep memory of days we did NOT look at this call; refresh the ones we did
    kept = {k for k in state.get("alerted", []) if k[:10] in dates and k[:10] not in day_free}
    state["alerted"] = sorted(kept | set(hits))

    print("Crystal checked:", ", ".join(sorted(day_free)),
          "| open:", sorted(hits) or "none", "| new:", new or "none", "| fresh:", fresh or "none")
    save_state(state)


if __name__ == "__main__":
    main()
