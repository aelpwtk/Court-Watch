import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import json, os, tempfile
from datetime import datetime, timezone, timedelta
os.environ["NTFY_TOPIC"] = ""
import doctor as d
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
def ts(mins_ago): return (NOW - timedelta(minutes=mins_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
n = [100]
def run(created, status="completed", conclusion="success", started=None, done=None):
    n[0] -= 1
    return {"databaseId": 1000 + n[0], "number": n[0], "status": status, "conclusion": conclusion if status == "completed" else "",
            "createdAt": ts(created), "startedAt": ts(started if started is not None else created), "updatedAt": ts(done if done is not None else created - 28)}
def go(runs, open_=None): return d.decide(runs, NOW, open_ or {}, lambda r: "Process completed with exit code 1.")
# 1 healthy: one running (fresh), one queued, recent successes -> nothing
r = [run(2, "queued"), run(12, "in_progress", started=11), run(40), run(70)]
a, al, o = go(r); assert not a and not al and not o, (a, al, o); print("1 healthy -> silent OK")
# 2 schedule skipped for 50 min, nothing active -> kick, no ping yet
r = [run(50), run(80), run(110)]
a, al, o = go(r); assert a == [("dispatch",)] and not al and not o, (a, al, o); print("2 gap 50 min -> kick quietly OK")
# 3 gap of 120 min -> kick + ping once; then no re-ping; then cleared ping
r = [run(120), run(150)]
a, al, o = go(r); assert ("dispatch",) in a and len(al) == 1 and "schedule stopped" in al[0][0] and "gap" in o, (a, al, o)
a2, al2, o2 = go(r, o); assert not al2 and "gap" in o2; print("3 gap 120 min -> ping once, no repeat OK")
r = [run(1, "queued"), run(30)]
a3, al3, o3 = go(r, o2); assert len(al3) == 1 and "cleared" in al3[0][0] and not o3; print("   cleared ping OK")
# 4 stuck run: running 50 min -> cancel
r = [run(2, "queued"), run(52, "in_progress", started=50), run(90)]
a, al, o = go(r); assert a == [("cancel", r[1]["databaseId"])], a; print("4 stuck 50 min -> cancel OK")
# 5 cancelled-by-concurrency runs are not failures
r = [run(2, "queued"), run(12, "in_progress", started=11), run(40, conclusion="cancelled"), run(60, conclusion="cancelled"), run(80)]
a, al, o = go(r); assert not a and not al and not o; print("5 cancelled runs ignored OK")
# 6 two failures in a row -> ping with note; one failure -> no ping
r = [run(2, "queued"), run(12, "in_progress", started=11), run(40, conclusion="failure"), run(70, conclusion="failure"), run(100)]
a, al, o = go(r); assert len(al) == 1 and "runs failing" in al[0][0] and "exit code 1" in al[0][1] and "fail" in o, (al, o)
r = [run(2, "queued"), run(12, "in_progress", started=11), run(40, conclusion="failure"), run(70)]
a, al, o = go(r); assert not al and not o; print("6 failure streak=2 pings, single failure doesn't OK")
# 7 all green but old success (runs completing as success but >2h ago can't happen unless gap) -> silent only when nothing else explains it
r = [run(2, "queued"), run(12, "in_progress", started=11), run(150, conclusion="failure"), run(180, conclusion="failure"), run(220)]
a, al, o = go(r); assert "fail" in o and "silent" not in o; print("7 failure explains silence (one ping) OK")
r = [run(2, "queued"), run(12, "in_progress", started=11), run(150, conclusion="timed_out"), run(180, conclusion="success", done=170)]
a, al, o = go(r); assert "silent" in o and "fail" not in o, o; print("8 silent when last success is old OK")
# 9 empty history -> kick
a, al, o = go([]); assert a == [("dispatch",)]; print("9 empty history -> kick OK")
# 10 end to end with fakes: state persists, ping failure retries
d.list_runs = lambda: [run(120), run(150)]; d.failure_note = lambda r: ""; calls = []
d.gh = lambda *a: calls.append(a) or ""; sent = []; d.ntfy = lambda t, b, p="default": sent.append(t) or True
os.chdir(tempfile.mkdtemp()); assert d.main() == 0 and ("workflow", "run", "court-watch.yml") in calls and sent
assert "gap" in json.load(open("doctor_state.json"))["open"]; sent.clear(); d.main(); assert not sent; print("10 end-to-end + dedupe OK")
d.ntfy = lambda t, b, p="default": False; os.remove("doctor_state.json"); d.main()
assert json.load(open("doctor_state.json"))["open"] == {}; print("11 failed ping -> state not advanced (retries) OK")
print("ALL PASS")
