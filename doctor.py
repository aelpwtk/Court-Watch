#!/usr/bin/env python3
"""Court-watch doctor: a rules-based caretaker that runs every ~20 min in its own workflow
(court-doctor.yml), so the bots keep running even when nobody is looking. No AI, no credits.

What it does (it only touches workflow RUNS, never your code, secrets or alert rules):
  GAP     no run queued/running and the newest started 35+ min ago (GitHub skipped the schedule)
          -> starts one. Pings you only if the gap passes 90 min.
  STUCK   a run has been going 45+ min (the job's own timeout is 35) -> cancels it.
  FAILING the last 2 finished runs both failed -> pings you with the failing step.
  SILENT  no successful run for 2 h (and no gap/failure already explained it) -> pings you.
Each problem pings once when it opens and once more when it clears (state in doctor_state.json).
Bot-level problems (a site down, a login failing) are already pinged by the bots themselves."""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

WORKFLOW = "court-watch.yml"
GAP_KICK_MIN = 35
GAP_ALERT_MIN = 90
STUCK_MIN = 45
SILENT_MIN = 120
FAIL_STREAK = 2
STATE_FILE = "doctor_state.json"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
REPO = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
BAD = {"failure", "timed_out", "startup_failure"}


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def minutes_since(ts, now):
    return (now - parse(ts)).total_seconds() / 60


# ---------- decisions (pure, unit-tested) ----------

def decide(runs, now, open_problems, failure_note=lambda run: ""):
    """runs: newest first, dicts from 'gh run list'. Returns (actions, alerts, still_open).
    actions: ("dispatch",) | ("cancel", run_id).  alerts: (title, body, priority)."""
    actions, alerts, still_open = [], [], {}
    active = [r for r in runs if r.get("status") in ACTIVE]
    done = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in ("cancelled", "skipped")]

    for r in active:                                            # STUCK
        if r.get("status") == "in_progress" and r.get("startedAt") and minutes_since(r["startedAt"], now) > STUCK_MIN:
            actions.append(("cancel", r["databaseId"]))

    gap_min = minutes_since(runs[0]["createdAt"], now) if runs else 10 ** 6   # GAP
    if not active and gap_min > GAP_KICK_MIN:
        actions.append(("dispatch",))
    if not active and gap_min > GAP_ALERT_MIN:
        still_open["gap"] = f"no run for {int(gap_min)} min"

    if len(done) >= FAIL_STREAK and all(r.get("conclusion") in BAD for r in done[:FAIL_STREAK]):   # FAILING
        n = ", ".join(f"#{r['number']}" for r in done[:FAIL_STREAK])
        still_open["fail"] = f"runs {n} failed. {failure_note(done[0])}".strip()

    if not (set(still_open) & {"gap", "fail"}):                 # SILENT (only if nothing else explains it)
        ok = next((r for r in done if r.get("conclusion") == "success"), None)
        age = minutes_since(ok["updatedAt"], now) if ok else 10 ** 6
        if age > SILENT_MIN:
            still_open["silent"] = f"no successful run for {int(age)} min" if ok else "no successful run on record"

    titles = {"gap": "schedule stopped", "fail": "runs failing", "silent": "bots quiet"}
    for key, detail in still_open.items():
        if key not in open_problems:
            alerts.append((f"\U0001fa7a Doctor — {titles[key]}", detail, "default"))
    for key in open_problems:
        if key not in still_open:
            alerts.append((f"\U0001fa7a Doctor — {titles[key]}: cleared", "Back to normal.", "low"))
    return actions, alerts, still_open


# ---------- plumbing ----------

def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def list_runs():
    out = gh("run", "list", "--workflow", WORKFLOW, "--limit", "14", "--json",
             "databaseId,number,status,conclusion,createdAt,startedAt,updatedAt")
    return json.loads(out)


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
    now = datetime.now(timezone.utc)
    state = load_state()
    try:
        runs = list_runs()
    except Exception as e:
        print(f"Doctor could not list runs: {e}")
        return 1
    actions, alerts, still_open = decide(runs, now, state.get("open", {}), failure_note)
    for act in actions:
        try:
            if act[0] == "dispatch":
                gh("workflow", "run", WORKFLOW)
                print("Doctor: no run queued or running - started one.")
            else:
                gh("run", "cancel", str(act[1]))
                print(f"Doctor: cancelled stuck run {act[1]}.")
        except Exception as e:
            print(f"Doctor action {act} failed: {e}")
    sent_all = all([ntfy(t, b, p) for t, b, p in alerts]) if alerts else True
    if sent_all:
        state["open"] = still_open       # if a ping failed to send, retry next time
    save_state(state)
    print(f"Doctor: {len(runs)} runs seen, actions={actions}, open={list(still_open)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
