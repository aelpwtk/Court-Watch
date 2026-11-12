#!/usr/bin/env python3
"""Court-watch doctor: a rules-based caretaker. No AI, no credits.

It runs as its own long loop (court-doctor.yml: one check every 3 min for ~2 h, then the next run takes
over), because GitHub only starts scheduled runs a few times a day. The watcher and the doctor keep each
other alive: every watcher run makes sure a doctor is running, and the doctor makes sure a watcher is.

What it does (it only touches workflow RUNS, never your code, secrets or alert rules):
  DOWN    no watcher run is working -> starts one. Two real cases from the logs: the next run was queued
          but never got a machine ("zombie"), or it failed before its first step. Zombies are cancelled.
          A hand-off between runs gets a few minutes of grace first.
          Pings you only if nothing ran for 30+ min.
  STUCK   a run's job has been going 45+ min (its own timeout is 35) -> cancels it.
  FAILING the last 2 finished runs both failed -> pings you with the failing step.
  SILENT  no successful run for 2 h (and nothing above already explained it) -> pings you.
Each problem pings once when it opens and once more when it clears (state in doctor_state.json).
Site-level problems (the court site down) are already pinged by the watcher itself.

Usage:  doctor.py                       one check of the watcher
        doctor.py --loop                check every DOCTOR_INTERVAL s for DOCTOR_LOOP_SECONDS
        doctor.py --ensure WORKFLOW MIN restart WORKFLOW if none of its runs is working (MIN = stuck limit)
        doctor.py --successor WORKFLOW  queue a run of WORKFLOW unless one is already waiting"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

WORKFLOW = "court-watch.yml"
GRACE_MIN = 6            # a hand-off (one run ends, the queued one starts) may take this long
GAP_ALERT_MIN = 30       # ping when nothing ran for this long
STUCK_MIN = 45           # watcher job timeout is 35
SILENT_MIN = 120
FAIL_STREAK = 2
LOOP_SECONDS = int(os.environ.get("DOCTOR_LOOP_SECONDS", 2 * 60 * 60))
INTERVAL = int(os.environ.get("DOCTOR_INTERVAL", 180))
STATE_FILE = "doctor_state.json"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
REPO = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
WAITING = {"queued", "pending", "waiting", "requested"}
BAD = {"failure", "timed_out", "startup_failure"}
NEVER = 10 ** 6


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def minutes_since(ts, now):
    return (now - parse(ts)).total_seconds() / 60


# ---------- decisions (pure, unit-tested) ----------

def liveness(runs, now, stuck_min=STUCK_MIN, grace_min=GRACE_MIN):
    """Is a run of this workflow actually working, and if not, what restarts it?
    runs: newest first, dicts from list_runs(). Returns (actions, healthy, down_min).
    actions: ("dispatch",) | ("cancel", run_id). down_min: minutes since anything last finished."""
    actions = []
    running = [r for r in runs if r.get("status") == "in_progress"]
    waiting = [r for r in runs if r.get("status") in WAITING]
    finished = [r for r in runs if r.get("status") == "completed"]

    # a run only counts from when its JOB started: a queued run's own clock starts ~28 min earlier
    stuck = [r for r in running
             if minutes_since(r.get("jobStartedAt") or r.get("updatedAt") or r["createdAt"], now) > stuck_min]
    for r in stuck:
        actions.append(("cancel", r["databaseId"]))
    if len(running) > len(stuck):
        return actions, True, 0.0                         # a healthy run is working

    down = min((minutes_since(r["updatedAt"], now) for r in finished), default=NEVER)
    if waiting:
        newest_wait = min(minutes_since(r["createdAt"], now) for r in waiting)
        if stuck or down <= grace_min or newest_wait <= grace_min:
            return actions, False, down                   # hand-off under way: give the next run a moment
        for r in waiting:                                 # waited too long with nothing running: zombies
            actions.append(("cancel", r["databaseId"]))
    actions.append(("dispatch",))
    return actions, False, down


def decide(runs, now, open_problems, failure_note=lambda run: ""):
    """Returns (actions, alerts, still_open). alerts: (title, body, priority)."""
    actions, healthy, down = liveness(runs, now)
    alerts, still_open = [], {}
    done = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in ("cancelled", "skipped")]

    if not healthy and (down > GAP_ALERT_MIN or "gap" in open_problems):               # DOWN
        still_open["gap"] = "no run on record" if down >= NEVER else f"nothing ran for {int(down)} min, restarting it"

    if len(done) >= FAIL_STREAK and all(r.get("conclusion") in BAD for r in done[:FAIL_STREAK]):   # FAILING
        n = ", ".join(f"#{r['number']}" for r in done[:FAIL_STREAK])
        still_open["fail"] = f"runs {n} failed. {failure_note(done[0])}".strip()

    if not (set(still_open) & {"gap", "fail"}):                 # SILENT (only if nothing else explains it)
        ok = next((r for r in done if r.get("conclusion") == "success"), None)
        age = minutes_since(ok["updatedAt"], now) if ok else NEVER
        if age > SILENT_MIN:
            still_open["silent"] = f"no successful run for {int(age)} min" if ok else "no successful run on record"

    titles = {"gap": "watcher stopped", "fail": "runs failing", "silent": "bots quiet"}
    for key, detail in still_open.items():
        if key not in open_problems:
            alerts.append((f"\U0001fa7a Doctor — {titles[key]}", detail, "default"))
    for key in open_problems:
        if key not in still_open:
            alerts.append((f"\U0001fa7a Doctor — {titles.get(key, key)}: cleared", "Back to normal.", "low"))
    return actions, alerts, still_open


# ---------- plumbing ----------

def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def job_started(run_id):
    """When the run's job really began (a run can sit queued for ~28 min before that)."""
    try:
        out = gh("api", f"repos/{REPO}/actions/runs/{run_id}/jobs", "-q", '.jobs[0].started_at // ""').strip()
        return out or None
    except Exception:
        return None


def list_runs(workflow=WORKFLOW):
    out = gh("run", "list", "--workflow", workflow, "--limit", "20", "--json",
             "databaseId,number,status,conclusion,createdAt,startedAt,updatedAt")
    runs = json.loads(out)
    for r in runs:
        if r.get("status") == "in_progress":
            r["jobStartedAt"] = job_started(r["databaseId"])
    return runs


def apply(actions, workflow=WORKFLOW):
    for act in actions:
        try:
            if act[0] == "dispatch":
                gh("workflow", "run", workflow)
                print(f"Doctor: no working run of {workflow} - started one.")
            else:
                try:
                    gh("run", "cancel", str(act[1]))
                except Exception:      # a zombie often ignores a normal cancel
                    gh("api", "-X", "POST", f"repos/{REPO}/actions/runs/{act[1]}/force-cancel")
                print(f"Doctor: cancelled stuck or zombie run {act[1]} of {workflow}.")
        except Exception as e:
            print(f"Doctor action {act} on {workflow} failed: {e}")


def failure_note(run):
    try:
        text = gh("run", "view", str(run["databaseId"]), "--log-failed")
    except Exception as e:
        return f"(could not read the log: {e})"
    lines = [l.split("Z ", 1)[-1].strip() for l in text.splitlines() if l.strip()]
    keep = [l for l in lines if "error" in l.lower() or "conflict" in l.lower() or "fatal" in l.lower()] or lines
    return " | ".join(keep[-3:])[:500]


def ntfy(title, message, priority="default"):
    if not TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return True
    body = {"topic": TOPIC, "title": title, "message": message,
            "priority": {"low": 2, "default": 3, "high": 4}.get(priority, 3), "tags": [],
            "click": f"https://github.com/{REPO}/actions" if REPO else ""}
    req = urllib.request.Request("https://ntfy.sh", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"ntfy failed: {e}")
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"open": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    """One check of the watcher."""
    now = datetime.now(timezone.utc)
    state = load_state()
    try:
        runs = list_runs()
    except Exception as e:
        print(f"Doctor could not list runs: {e}")
        return 1
    actions, alerts, still_open = decide(runs, now, state.get("open", {}), failure_note)
    apply(actions)
    sent_all = all([ntfy(t, b, p) for t, b, p in alerts]) if alerts else True
    if sent_all:
        state["open"] = still_open       # if a ping failed to send, retry next time
    save_state(state)
    print(f"Doctor: {len(runs)} runs seen, actions={actions}, open={list(still_open)}")
    return 0


def loop():
    end = time.monotonic() + LOOP_SECONDS
    n = 0
    while True:
        n += 1
        try:
            main()
        except Exception as e:           # one bad check must never stop the loop
            print(f"Doctor check failed: {e}")
        if time.monotonic() + INTERVAL >= end:
            break
        time.sleep(INTERVAL)
    print(f"Doctor: {n} checks this run.")
    return 0


def ensure(workflow, stuck_min, now=None):
    """Restart WORKFLOW if none of its runs is working. Used by the watcher to keep the doctor alive."""
    actions, healthy, down = liveness(list_runs(workflow), now or datetime.now(timezone.utc), stuck_min)
    apply(actions, workflow)
    print(f"ensure {workflow}: healthy={healthy}, actions={actions}")
    return 0


def successor(workflow):
    """Queue the next run of WORKFLOW unless one is already waiting (belt and braces for self-chaining)."""
    if any(r.get("status") in WAITING for r in list_runs(workflow)):
        print(f"successor {workflow}: already queued.")
    else:
        apply([("dispatch",)], workflow)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--loop"]:
        sys.exit(loop())
    if args[:1] == ["--ensure"]:
        sys.exit(ensure(args[1], float(args[2])))
    if args[:1] == ["--successor"]:
        sys.exit(successor(args[1]))
    sys.exit(main())
