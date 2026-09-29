#!/usr/bin/env python3
"""Ace of Clubs watch: logs in, reads the booking grid and pings your phone
(ntfy) when a court frees up in your windows:
  Mon-Fri 19:00-22:00, Sat-Sun 09:00-22:00 (any 1-hour slot counts;
  2 hours in a row is flagged - switching courts at the hour is fine).
Runs on GitHub Actions every ~10 minutes."""

import http.cookiejar
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://aceofclubsbkk.com"
LOGIN_URL = BASE + "/my-account/"
BOOKING_URL = BASE + "/booking/"
WEEKDAY_HOURS = range(19, 22)   # slot start times 19:00, 20:00, 21:00 (ends 22:00)
WEEKEND_HOURS = range(9, 22)    # slot start times 09:00 ... 21:00 (ends 22:00)
STATE_FILE = "state_ace.json"
FAIL_ALERT_AFTER = 6            # ~1 hour of failed checks before it warns you
BKK = timezone(timedelta(hours=7))
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
USER = os.environ.get("ACE_USER", "").strip()
PASSWORD = os.environ.get("ACE_PASS", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class AuthError(Exception):
    pass


# ---------- notifications & state ----------

def ntfy(title, message, priority="urgent", tags="tennis"):
    if not TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": BOOKING_URL},
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

def make_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]
    return opener


def get(opener, url, data=None):
    with opener.open(url, data=data, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def form_value(page, name):
    tag = re.search(r'<[^>]*\bname="%s"[^>]*>' % re.escape(name), page)
    if not tag:
        return None
    val = re.search(r'\bvalue="([^"]*)"', tag.group(0))
    return val.group(1) if val else ""


def login_and_fetch_booking():
    if not USER or not PASSWORD:
        raise AuthError("ACE_USER / ACE_PASS secrets are missing")
    opener = make_opener()
    page = get(opener, LOGIN_URL)
    nonce = form_value(page, "woocommerce-login-nonce")
    if not nonce:
        raise RuntimeError("Login form not found - site changed or the bot is being blocked")
    form = {
        "username": USER,
        "password": PASSWORD,
        "rememberme": "forever",
        "woocommerce-login-nonce": nonce,
        "_wp_http_referer": form_value(page, "_wp_http_referer") or "/my-account/",
        "login": form_value(page, "login") or "Log in",
    }
    after = get(opener, LOGIN_URL, urllib.parse.urlencode(form).encode())
    booking = get(opener, BOOKING_URL)
    if "var blockedEvents" in booking and "defaultDate:" in booking:
        return booking
    err = re.search(r'class="woocommerce-error"[^>]*>(.*?)</ul>', after, re.S)
    if err:
        msg = re.sub(r"<[^>]+>", " ", err.group(1))
        raise AuthError("Login rejected: " + " ".join(msg.split())[:200])
    raise AuthError("Login didn't stick - the site may be blocking automated logins")


def js_value(page, var, default=None):
    m = re.search(r"var\s+%s\s*=\s*" % re.escape(var), page)
    if not m:
        if default is not None:
            return default
        raise RuntimeError(f"'{var}' not found - the site layout may have changed")
    value, _ = json.JSONDecoder().raw_decode(page, m.end())
    return value


def bookable_dates(page):
    # The date buttons are JS templates (data-date="${dateFormat}") filled in by the
    # browser, so we can't read them from raw HTML. Instead the server prints the start
    # date, and the buttons run from there for advance_booking_limit days.
    m = (re.search(r"defaultDate:\s*'(\d{4}-\d{2}-\d{2})'", page)
         or re.search(r"now:\s*'(\d{4}-\d{2}-\d{2})'", page))
    if not m:
        raise RuntimeError("Booking start date not found - the site layout may have changed")
    limit = re.search(r"advance_booking_limit\s*=\s*(\d+)", page)
    days = int(limit.group(1)) if limit else 7
    d0 = datetime.strptime(m.group(1), "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def find_free(page, now):
    courts = {str(c["id"]): c["title"] for c in js_value(page, "aceResources")["court"]}
    taken = set()
    for var in ("blockedEvents", "ogcEvents"):
        for e in js_value(page, var, default=[]):
            if e.get("resourceId") and e.get("start"):
                taken.add((str(e["resourceId"]), e["start"][:13]))   # "2026-10-03 19"
    holidays = set(js_value(page, "holiday", default=[]))
    dates = bookable_dates(page)
    today = now.strftime("%Y-%m-%d")

    free = {}   # "2026-10-03 19" -> ["Court 2", "Court 5"]
    for ds in dates:
        if ds in holidays:
            continue
        weekday = datetime.strptime(ds, "%Y-%m-%d").weekday()
        for h in (WEEKEND_HOURS if weekday >= 5 else WEEKDAY_HOURS):
            if ds == today and h <= now.hour:
                continue   # already started
            key = f"{ds} {h:02d}"
            open_courts = [title for cid, title in courts.items() if (cid, key) not in taken]
            if open_courts:
                free[key] = open_courts
    return free, dates


# ---------- message ----------

def nice_date(ds):
    return datetime.strptime(ds, "%Y-%m-%d").strftime("%a %d/%m")


def short_courts(titles):
    nums = [t.replace("Court ", "") for t in titles]
    return ("Court " if len(nums) == 1 else "Courts ") + "/".join(nums)


def two_hour_runs(free, new_keys):
    runs = []
    for key in sorted(free):
        ds, h = key[:10], int(key[11:])
        nxt = f"{ds} {h + 1:02d}"
        if nxt in free and (key in new_keys or nxt in new_keys):
            same = sorted(set(free[key]) & set(free[nxt]))
            how = (f"{short_courts(same)} both hours" if same
                   else f"{short_courts(free[key])} > {short_courts(free[nxt])}")
            runs.append(f"{nice_date(ds)} {h:02d}:00-{h + 2:02d}:00 - {how}")
    return runs


def build_message(free, new_keys):
    lines = [f"{nice_date(k[:10])} {k[11:]}:00 - {short_courts(free[k])}" for k in sorted(new_keys)]
    runs = two_hour_runs(free, set(new_keys))
    if runs:
        lines += ["", "2 HOURS STRAIGHT:"] + runs
    lines += ["", "Book it now - tap to open."]
    title = "Ace: 2 HOURS open!" if runs else f"Ace: court open ({len(new_keys)})"
    return title, "\n".join(lines)


# ---------- main ----------

def main():
    if not USER or not PASSWORD:
        print("Waiting for the ACE_USER / ACE_PASS secrets - skipping Ace of Clubs for now.")
        return
    state, first_run = load_state()
    now = datetime.now(BKK)

    try:
        free, dates = find_free(login_and_fetch_booking(), now)
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        print(f"Check failed ({state['fails']} in a row): {e}")
        # a bad password won't fix itself, so warn right away; other errors after ~1 hour
        if (isinstance(e, AuthError) or state["fails"] >= FAIL_ALERT_AFTER) and not state.get("fail_alerted"):
            if safe_ntfy("Ace bot is stuck", f"{e}", priority="default", tags="warning"):
                state["fail_alerted"] = True
        save_state(state)
        return

    if state.get("fail_alerted"):
        safe_ntfy("Ace bot is back", "Checks are working again.", priority="default", tags="white_check_mark")
    state["fails"] = 0
    state["fail_alerted"] = False

    if first_run:
        safe_ntfy("Ace bot is live",
                  "Watching Mon-Fri 19:00-22:00 and Sat-Sun 09:00-22:00, all courts, every ~10 min.",
                  priority="default", tags="white_check_mark")

    # Learn when the club opens a new booking day (reported once)
    if dates:
        last = state.get("window_last")
        if last and dates[-1] > last and not state.get("release_reported"):
            if safe_ntfy("Ace: new day just opened",
                         f"{nice_date(dates[-1])} became bookable between the last check and "
                         f"{now.strftime('%H:%M')}. That's roughly when new days release - "
                         "be on the booking page around then.",
                         priority="default", tags="calendar"):
                state["release_reported"] = True
        if last and dates[-1] > last:
            state["last_release_seen"] = now.strftime("%Y-%m-%d %H:%M")
        state["window_last"] = dates[-1]

    already = set(state.get("alerted", []))
    new = [k for k in sorted(free) if k not in already]
    if new:
        title, body = build_message(free, new)
        if not safe_ntfy(title, body):
            free = {k: v for k, v in free.items() if k not in new}   # retry next run

    print("Bookable window:", dates[0] if dates else "?", "to", dates[-1] if dates else "?")
    print("Free in your windows:", {k: v for k, v in sorted(free.items())} or "none", "| new:", new or "none")
    state["alerted"] = sorted(free)   # slots that get booked drop off, so a re-open pings again
    save_state(state)


if __name__ == "__main__":
    main()
