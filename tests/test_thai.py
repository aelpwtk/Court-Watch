# -*- coding: utf-8 -*-
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["NTFY_TOPIC"] = "t"
import check_courts as c

assert c.th_date("Tue 29/09") == "อังคาร 29/09", c.th_date("Tue 29/09")
assert c.th_date("Sun 04/10") == "อาทิตย์ 04/10"
assert c.th_date("Thu 01/10") == "พฤหัสบดี 01/10"
assert c.th_date("Xyz 01/10") == "Xyz 01/10"          # unknown text passes through

def data(free_by_day):
    days = list(free_by_day)
    booking = {}
    for i, (_, courts_free) in enumerate(free_by_day.items(), start=1):
        booking[str(i)] = {str(ct): {"1": ct not in courts_free[0], "2": ct not in courts_free[1]} for ct in range(1, 7)}
    return {"timeLabels": {"0": "20.00-21.00", "1": "21.00-22.00"},
            "dateInfos": [{"dateStr": d, "dayNum": i} for i, d in enumerate(days, start=1)],
            "bookingMap": booking}

from datetime import datetime
now = datetime(2026, 10, 19, 12, 0, tzinfo=c.BKK)
hits, all_dates = c.find_hits(data({"Tue 20/10": ([3, 4], [3]), "Wed 21/10": ([1], [2])}), now)
assert hits["Tue 20/10"] == "อังคาร 20/10 · คอร์ท 3 (ทั้ง 2 ชม.)", hits
assert hits["Wed 21/10"] == "พุธ 21/10 · 20:00 คอร์ท 1 → 21:00 คอร์ท 2", hits
assert list(hits) == ["Tue 20/10", "Wed 21/10"]         # state keys stay the site's English text

# full alert path: capture what would be sent
sent = []
c.ntfy = lambda title, msg, **kw: sent.append((title, msg, kw))
tmp = tempfile.mkdtemp(); c.STATE_FILE = os.path.join(tmp, "s.json")
c.fetch = lambda: data({"Tue 20/10": ([3], [3])})
c.main()                                                  # first run: 'now watching' (English health ping) + alert
alerts = [s for s in sent if s[2].get("share")]
assert len(alerts) == 1, sent
assert alerts[0][0] == "\U0001f6a8\U0001f3be คอร์ทว่างแล้วจ้าวไอต้าว", alerts[0][0]
assert "คอร์ทว่าง" in alerts[0][0] and "อังคาร 20/10" in alerts[0][1] and "ใครมาก่อนได้ก่อน" in alerts[0][1], alerts
assert not any(s[2].get("share") for s in sent if "now watching" in s[0])   # health ping never shared
print("ALL PASS")
