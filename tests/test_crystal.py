import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import os, json, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["CRYSTAL_GAP"]="0"; os.environ["NTFY_TOPIC"]="x"
import check_crystal as cc
BKK=cc.BKK
sent=[]
cc.ntfy=lambda t,m,priority="urgent",tags="":sent.append((t,m))
class FakeDT(datetime):
    NOW=datetime(2026,10,8,0,30,tzinfo=BKK)
    @classmethod
    def now(cls,tz=None): return cls.NOW
cc.datetime=FakeDT
HOURS=range(6,24)
# real captured Sat-17 LOC001 grid: everything taken except a few edge hours
REAL={"Center-1":"011111111111111111","Center-2":"111111111111111110","North-1":"111111111111111110",
      "North-2":"111111111111111111","North-3":"111111111111111110","South-1":"111111111111111111",
      "South-2":"111111111111111111","South-3":"111111111111111110"}
FREEMAP={}   # (date,loc,court,hour)->free
def rows(date,loc):
    names={"LOC001":list(REAL),"LOC002":["G North-1","G South-1"]}[loc]
    out=[]
    for c in names:
        for i,h in enumerate(HOURS):
            if loc=="LOC001" and c in REAL: base=REAL[c][i]
            else: base="1"
            st="0" if FREEMAP.get((date,loc,c,h)) else base
            out.append({"stadiumName":c,"timeStart":f"{h:02d}:00:00","reservestatus":st,"locId":loc})
    return out
calls=[]
def fake_fetch(d,l): calls.append((d,l)); return rows(d,l)
cc.fetch=fake_fetch
d=tempfile.mkdtemp(); os.chdir(d)
def run():
    sent.clear(); cc.main(); return list(sent)
# 1 first run
r=run(); print("1 first run:",[t for t,_ in r]); assert [t for t,_ in r]==["\U0001f48e Crystal — now watching"], r
print("   checked:",sorted(set(c[0] for c in calls)),"calls",len(calls))
# 2 weekend 2h straight Sat 10 Oct 19,20
cc._tick=0
for h in (19,20): FREEMAP[("2026-10-10","LOC001","Center-2",h)]=True
for k in range(12):
    r=run()
    if r: break
print("2 weekend:",r); assert r and "2 hrs" in r[0][0] and "Sat 10/10" in r[0][1]
# 3 no repeat
for k in range(12):
    assert not run(), "repeat ping"
print("3 no repeat OK")
# 4 weekday: only 20h free Mon 12 -> no; add 21h -> alert
cc._tick=0
FREEMAP[("2026-10-12","LOC001","North-1",20)]=True
for k in range(12): assert not run()
FREEMAP[("2026-10-12","LOC002","G South-1",21)]=True
for k in range(12):
    r=run()
    if r: break
print("4 weekday:",r); assert r and "Mon 12/10" in r[0][1] and "20h North-1" in r[0][1] and "G South-1" in r[0][1]
# 5 slot gets booked then reopens -> pings again
FREEMAP.pop(("2026-10-12","LOC001","North-1",20))
for k in range(12): run()
st=json.load(open("state_crystal.json")); assert "2026-10-12" not in st["alerted"], st
FREEMAP[("2026-10-12","LOC001","North-1",20)]=True
for k in range(12):
    r=run()
    if r: break
assert r; print("5 reopen pings again OK")
# 6 fresh radar: pretend newest day (Thu 22/10) wasn't known and is open
FREEMAP.clear()
st=json.load(open("state_crystal.json")); st["alerted"]=[]; st["known_days"]=[x for x in st["known_days"] if x!="2026-10-21"]
json.dump(st,open("state_crystal.json","w"))
FREEMAP[("2026-10-21","LOC001","North-2",9)]=True   # Wed 21 Oct weekday? check
dt=datetime.strptime("2026-10-21","%Y-%m-%d"); print("   21 Oct weekday idx",dt.weekday())
FREEMAP[("2026-10-21","LOC001","North-2",20)]=True; FREEMAP[("2026-10-21","LOC001","North-2",21)]=True
r=run(); print("6 fresh:",r); assert r and "new day" in r[0][0]
# 7 rate limit
def rl(d,l): raise cc.RateLimited("RATE_LIMITED")
cc.fetch=rl; before=open("state_crystal.json").read(); r=run(); assert not r and open("state_crystal.json").read()==before
assert cc._backoff_until>time.monotonic(); cc.fetch=fake_fetch; n=len(calls); run(); assert len(calls)==n; print("7 rate limit backoff OK")
cc._backoff_until=0; cc._safe_until=0; cc._rl_times.clear()
# 8 failures
def bad(d,l): raise RuntimeError("boom")
cc.fetch=bad
for i in range(cc.FAIL_ALERT_AFTER):
    cc._err_until=0; r=run()      # (time passing between probes)
assert any("stuck" in t for t,_ in r), r; print("8 fail alert after",cc.FAIL_ALERT_AFTER,"OK")
cc._err_until=0; cc._err_streak=0
cc.fetch=fake_fetch; r=run(); assert any("back online" in t for t,_ in r); print("   back online OK")
# 9 rotation coverage
calls.clear(); cc._tick=0
seen=set()
for i in range(10): cc.main(); 
seen={c[0] for c in calls}; print("9 rotation covers",len(seen),"of 14 days in 5 calls; newest checked",sum(1 for c in calls if c[0]=="2026-10-21" and c[1]=="LOC001"),"times")
assert len(seen)==14
# 10 both-venue rule: LOC002 failing -> day untrusted
def half(d,l):
    if l=="LOC002": raise RuntimeError("x")
    return rows(d,l)
cc.fetch=half; n=len(sent); st0=open("state_crystal.json").read(); cc.main(); print("10 half-failure -> fails counted:",json.load(open("state_crystal.json"))["fails"])

# 11 fast lane request counts per tick
cc.fetch=fake_fetch; cc._safe_until=0; cc._backoff_until=0; cc._tick=0; cc._round=0; cc.fetch=half if False else fake_fetch
cc.fetch=fake_fetch
per=[]
for i in range(6):
    calls.clear(); cc.main(); per.append(len(calls))
print("11 requests per tick (fast):",per)
assert per[0]>per[1] and per[3]>per[1]
wk=lambda: {c[0] for c in calls}
calls.clear(); cc._tick=1; cc.main(); d=wk(); print("   non-rotation tick days:",sorted(d))
assert all(datetime.strptime(x,"%Y-%m-%d").weekday()>=5 or x=="2026-10-21" for x in d)
# 12 escalation + safe mode
cc._tick=0; cc._round=0; cc._safe_until=0; cc._backoff_until=0; cc._rl_times.clear(); sent.clear()
cc.fetch=rl; cc.main(); b1=cc._backoff_until-time.monotonic(); assert 60<b1<=65 and not sent, (b1,sent)
cc._backoff_until=0; cc._safe_until=0; cc.main(); b2=cc._backoff_until-time.monotonic(); assert 290<b2<=300, b2; assert sent and "pushed back" in sent[0][0], sent
cc._backoff_until=0; cc._safe_until=0; cc.main(); b3=cc._backoff_until-time.monotonic(); assert 890<b3<=900, b3
print("12 backoff escalation 65/300/900 OK, warning sent once at 2nd hit")
# 13 safe mode = gentle pace (1 round in 3)
cc.fetch=fake_fetch; cc._backoff_until=0; cc._round=0; cc._tick=0
assert cc._safe_until>time.monotonic()
ran=[]
for i in range(6):
    calls.clear(); cc.main(); ran.append(len(calls)>0)
print("13 safe mode rounds that fetched:",ran); assert ran==[True,False,False,True,False,False]
# 14 safe mode expiry -> fast again
cc._safe_until=0; calls.clear(); cc.main(); assert calls; print("14 back to fast after safe window OK")
# 15 kill switch
cc.FAST=False; cc._round=0; cc._tick=0; ran=[]
for i in range(3):
    calls.clear(); cc.main(); ran.append(len(calls)>0)
assert ran==[True,False,False]; cc.FAST=True; print("15 kill switch OK")

# 16 weekend: single free hour -> no alert; two consecutive hours (court switch) -> alert; outside window ignored
import importlib
cc.fetch=fake_fetch; cc._safe_until=0; cc._backoff_until=0; cc._rl_times.clear(); cc._round=0; cc._tick=0
FREEMAP.clear(); os.remove("state_crystal.json") if os.path.exists("state_crystal.json") else None
sent.clear(); cc.main(); sent.clear()   # first-run now-watching
FREEMAP[("2026-10-17","LOC001","North-1",15)]=True   # Sat 17, lone hour
for k in range(6): assert not run(), "lone hour alerted"
print("16a lone weekend hour -> no alert OK")
FREEMAP[("2026-10-17","LOC001","South-1",16)]=True   # different court next hour
for k in range(6):
    r=run()
    if r: break
print("16b",r); assert r and "Sat 17/10" in r[0][1] and "15h North-1" in r[0][1] and "16h South-1" in r[0][1] and "2 hrs open" in r[0][0]
# 3 in a row 15,16,17 -> adds block 16 (new) only once
FREEMAP[("2026-10-17","LOC001","South-1",17)]=True
for k in range(6):
    r=run()
    if r: break
print("16c",r); assert r and "16" in r[0][1]
# edge: 21:00 alone (block would end 23:00) -> no alert
FREEMAP.clear(); 
for k in range(3): run()
FREEMAP[("2026-10-18","LOC001","North-1",21)]=True
for k in range(6): assert not run()
FREEMAP[("2026-10-18","LOC001","North-1",20)]=True
for k in range(6):
    r=run()
    if r: break
print("16d",r); assert r and "20:00-22:00 North-1 (both hours)" in r[0][1]

# 17 site outage (HTTP 500): round stops after 2 failed days, then slow probing of the newest day only
import urllib.error
def e500(d,l): calls.append((d,l)); raise urllib.error.HTTPError("u",500,"Internal Server Error",{},None)
cc.fetch=e500; cc._err_streak=0; cc._err_until=0; cc._round=0; cc._tick=0; cc._safe_until=0; cc._backoff_until=0
sent.clear(); calls.clear(); cc.main(); assert len(calls)<=2, calls
print("17a round aborted after",len(calls),"failed requests (was ~10)")
for _ in range(2): cc._err_until=0; cc.main()
assert cc._err_streak==3 and cc._err_until>time.monotonic(), (cc._err_streak, cc._err_until)
calls.clear(); cc.main(); assert not calls; print("17b quiet while waiting between probes OK")
cc._err_until=0; calls.clear(); cc.main()
assert {c[0] for c in calls}=={"2026-10-21"} and len(calls)==1, calls; print("17c probe = newest day only, 1 request OK")
# 18 time-based alert: >=20 min without data -> one 'site is down' ping with real duration
st=json.load(open("state_crystal.json")); st["fail_since"]=int(time.time())-21*60; st["fail_alerted"]=False; st["fails"]=5
json.dump(st,open("state_crystal.json","w"))
cc._err_until=0; sent.clear(); cc.main()
assert sent and "site is down" in sent[0][0] and "server is erroring" in sent[0][1] and "~21 min" in sent[0][1], sent
cc._err_until=0; sent.clear(); cc.main(); assert not sent; print("18 time-based 'site is down' ping once, with duration OK:",sent or "no repeat")
# 19 recovery: back-online ping, counters cleared, full rounds resume
cc.fetch=fake_fetch; cc._err_until=0; sent.clear(); cc.main()
assert any("back online" in t for t,_ in sent), sent
assert cc._err_streak==0 and "fail_since" not in json.load(open("state_crystal.json"))
calls.clear(); cc.main(); assert len({c[0] for c in calls})>1; print("19 recovery + full rounds resume OK")
# 20 non-HTTP failure keeps the 'bot stuck' wording
cc.fetch=bad; cc._err_streak=0; cc._err_until=0
st=json.load(open("state_crystal.json")); st["fail_since"]=int(time.time())-25*60; st["fail_alerted"]=False
json.dump(st,open("state_crystal.json","w")); sent.clear(); cc.main()
assert sent and "bot stuck" in sent[0][0] and "~25 min" in sent[0][1], sent; print("20 bot-stuck wording for non-site errors OK")
cc.fetch=fake_fetch; cc._err_until=0; cc._err_streak=0
# 21 alert text carries the click path
t,b=cc.build_message({"2026-10-17 15":{"kind":"block","h":15,"a":["North-1"],"b":["South-1"]}},["2026-10-17 15"])
assert "date" in b and "venue tab" in b and "court" in b; print("21 click-path hint in alert OK")
print("ALL PASS")
