import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import json, os, tempfile
from datetime import datetime, timezone, timedelta
os.environ["NTFY_TOPIC"] = ""
import doctor as d
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
def ts(mins_ago): return (NOW - timedelta(minutes=mins_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
n = [100]
def run(created, status="completed", conclusion="success", job=None, done=None):
    """created = minutes ago the run was queued; job = minutes ago its job really started; done = minutes ago it ended."""
    n[0] -= 1
    r = {"databaseId": 1000 + n[0], "number": n[0], "status": status, "conclusion": conclusion if status == "completed" else "",
         "createdAt": ts(created), "startedAt": ts(created),
         "updatedAt": ts(done if done is not None else (job if job is not None else max(created - 28, 0)))}
    if status == "in_progress":
        r["jobStartedAt"] = ts(job) if job is not None else None
    return r
def go(runs, open_=None): return d.decide(runs, NOW, open_ or {}, lambda r: "Process completed with exit code 1.")
DISPATCH = ("dispatch",)

# 1 healthy chain: one working (queued 40 min ago, job began 12 min ago), its successor pending -> nothing
r = [run(12, "pending"), run(40, "in_progress", job=12), run(68, done=12), run(96, done=40)]
a, al, o = go(r); assert not a and not al and not o, (a, al, o); print("1 healthy -> silent OK")

# 2 THE BUG THAT KILLED 15 HEALTHY RUNS: a run queued 50 min ago whose job began 22 min ago is NOT stuck
r = [run(22, "pending"), run(50, "in_progress", job=22), run(78, done=22)]
a, al, o = go(r); assert not a and not al and not o, (a, al, o); print("2 long-queued but fresh job -> not cancelled OK")

# 3 really stuck: job going 50 min -> cancel it; the pending successor takes over, so no extra dispatch
r = [run(50, "pending"), run(78, "in_progress", job=50), run(110, done=50)]
a, al, o = go(r); assert a == [("cancel", r[1]["databaseId"])], a; print("3 job stuck 50 min -> cancel only OK")
#   stuck with nothing waiting -> cancel and start a new one
r = [run(78, "in_progress", job=50), run(110, done=50)]
a, al, o = go(r); assert a == [("cancel", r[0]["databaseId"]), DISPATCH], a; print("   stuck, no successor -> cancel + dispatch OK")
#   job start unknown: fall back to the run's last update
r = [run(78, "in_progress", done=50)]
a, al, o = go(r); assert ("cancel", r[0]["databaseId"]) in a, a; print("   unknown job start, stale -> cancel OK")

# 4 hand-off: previous run ended 1 min ago, successor still queued -> wait (grace)
r = [run(29, "queued"), run(57, done=1), run(85, done=29)]
a, al, o = go(r); assert not a and not al and not o, (a, al, o); print("4 hand-off within grace -> wait OK")

# 5 ZOMBIE (4 Nov outage): successor queued, nothing running, last run ended 20 min ago -> cancel zombie + dispatch, no ping yet
r = [run(48, "queued"), run(76, done=20), run(104, done=48)]
a, al, o = go(r); assert a == [("cancel", r[0]["databaseId"]), DISPATCH] and not al and not o, (a, al, o); print("5 zombie successor -> cancel + dispatch OK")
#   the run we just dispatched is 2 min old and still queued -> wait, don't dispatch again
r = [run(2, "queued"), run(76, done=22)]
a, al, o = go(r); assert not a, a; print("   freshly dispatched run -> wait OK")

# 6 DEAD START (12 Nov outage): successor failed before its first step, nothing waiting -> dispatch at once
r = [run(28, conclusion="failure", done=0), run(56, done=0), run(84, done=28)]
a, al, o = go(r); assert a == [DISPATCH] and not al, (a, al); print("6 successor failed at start -> dispatch at once OK")

# 7 long outage: nothing ran for 120 min -> dispatch + ping once; stays open (no flapping) while restarting; clears when a run works
r = [run(148, done=120), run(176, done=148)]
a, al, o = go(r); assert DISPATCH in a and len(al) == 1 and "watcher stopped" in al[0][0] and "120 min" in al[0][1] and "gap" in o, (a, al, o)
r2 = [run(2, "queued")] + r
a2, al2, o2 = go(r2, o); assert not a2 and not al2 and "gap" in o2, (a2, al2, o2); print("7 outage 120 min -> ping once, stays open during restart OK")
r3 = [run(30, "pending"), run(5, "in_progress", job=3)] + r
a3, al3, o3 = go(r3, o2); assert not a3 and len(al3) == 1 and "cleared" in al3[0][0] and not o3, (a3, al3, o3); print("   cleared ping when a run works again OK")
#   short downtime (10 min) restarts quietly
r = [run(38, done=10)]
a, al, o = go(r); assert a == [DISPATCH] and not al and not o, (a, al, o); print("   10 min down -> restart quietly OK")

# 8 cancelled-by-concurrency runs are not failures
r = [run(12, "pending"), run(40, "in_progress", job=12), run(45, conclusion="cancelled", done=44), run(68, done=12)]
a, al, o = go(r); assert not a and not al and not o; print("8 cancelled runs ignored OK")

# 9 two failures in a row -> ping with note; one failure -> no ping
ok = [run(12, "pending"), run(40, "in_progress", job=12)]
r = ok + [run(68, conclusion="failure", done=12), run(96, conclusion="failure", done=40), run(124, done=68)]
a, al, o = go(r); assert len(al) == 1 and "runs failing" in al[0][0] and "exit code 1" in al[0][1] and "fail" in o, (al, o)
r = ok + [run(68, conclusion="failure", done=12), run(96, done=40)]
a, al, o = go(r); assert not al and not o; print("9 failure streak=2 pings, single failure doesn't OK")

# 10 silent: runs keep going but the last success is old; a failure streak explains it instead
r = ok + [run(180, conclusion="failure", done=150), run(210, conclusion="failure", done=180), run(250, done=220)]
a, al, o = go(r); assert "fail" in o and "silent" not in o
r = ok + [run(180, conclusion="timed_out", done=150), run(210, done=170)]
a, al, o = go(r); assert "silent" in o and "fail" not in o, o; print("10 silent only when nothing else explains it OK")

# 11 empty history -> start one
a, al, o = go([]); assert a == [DISPATCH] and "gap" in o; print("11 empty history -> dispatch OK")

# 12 liveness for the doctor's own chain uses its own (longer) stuck limit
r = [run(100, "in_progress", job=100)]
assert d.liveness(r, NOW, stuck_min=150) == ([], True, 0.0)
assert d.liveness(r, NOW, stuck_min=90)[0] == [("cancel", r[0]["databaseId"]), DISPATCH]; print("12 per-workflow stuck limit OK")

# 13 end to end with fakes: state persists, dedupe, ping failure retries, zombie force-cancel fallback
old = [run(148, done=120), run(176, done=148)]
d.list_runs = lambda workflow=d.WORKFLOW: old; d.failure_note = lambda r: ""; calls = []
d.gh = lambda *a: calls.append(a) or ""; sent = []; d.ntfy = lambda t, b, p="default": sent.append(t) or True
os.chdir(tempfile.mkdtemp()); assert d.main() == 0 and ("workflow", "run", "court-watch.yml") in calls and sent
assert "gap" in json.load(open("doctor_state.json"))["open"]; sent.clear(); d.main(); assert not sent; print("13 end-to-end + dedupe OK")
d.ntfy = lambda t, b, p="default": False; os.remove("doctor_state.json"); d.main()
assert json.load(open("doctor_state.json"))["open"] == {}; print("   failed ping -> state not advanced (retries) OK")
calls.clear()
def gh_cancel_fails(*a):
    calls.append(a)
    if a[:2] == ("run", "cancel"): raise RuntimeError("cannot cancel")
    return ""
d.gh = gh_cancel_fails; d.apply([("cancel", 77)])
assert any(c[0] == "api" and c[-1].endswith("/actions/runs/77/force-cancel") for c in calls), calls; print("   zombie: force-cancel fallback OK")

# 14 --ensure restarts another workflow only when none of its runs is working
calls.clear(); d.gh = lambda *a: calls.append(a) or ""
d.list_runs = lambda workflow=d.WORKFLOW: [run(200, done=170)]
d.ensure("court-doctor.yml", 150, NOW); assert ("workflow", "run", "court-doctor.yml") in calls, calls
calls.clear(); d.list_runs = lambda workflow=d.WORKFLOW: [run(100, "in_progress", job=100)]
d.ensure("court-doctor.yml", 150, NOW); assert not calls, calls; print("14 ensure helper OK")

# 15 the loop keeps going when a check blows up
ticks = []
def boom():
    ticks.append(1)
    if len(ticks) == 1: raise RuntimeError("api down")
    return 0
d.main = boom; d.LOOP_SECONDS = 1; d.INTERVAL = 0.3; assert d.loop() == 0 and len(ticks) >= 2, ticks; print("15 loop survives a failed check OK")
print("ALL PASS")
