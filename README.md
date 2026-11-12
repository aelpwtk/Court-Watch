# Court-Watch

Pings Marc's phone (ntfy) when The Challenge Tennis Club has a court free for both 20:00-21:00 and
21:00-22:00 on the same day. Court alerts also go to a second, shared ntfy topic.

## How it runs

| Part | File | What it does |
|---|---|---|
| Watcher | `check_courts.py`, `watch_loop.py`, `.github/workflows/court-watch.yml` | Checks the court page every 20 s in ~28 min runs. Each run queues the next one. |
| Doctor | `doctor.py`, `.github/workflows/court-doctor.yml` | Checks every 3 min that a watcher run is really working. Restarts it, cancels stuck or zombie runs, pings on repeated failures. Runs ~2 h at a time and queues its own next run. |
| Test ping | `.github/workflows/ntfy-test.yml` | Manual: sends one test notification to both topics. |
| Repair agent | `AGENT.md` | Playbook for the hourly Claude scheduled task that sits outside GitHub. |

The watcher and the doctor restart each other, so both have to die at the same moment for the bot to go
dark. GitHub's own schedule (cron) only fires a few times a day on this repo and is kept as a last backup.

## Secrets

`NTFY_TOPIC` (Marc's topic: court alerts and health pings) and `NTFY_TOPIC_SHARED` (court alerts only).

## Tests

`sh tests/run_all.sh` must print `ALL TESTS PASSED` before any push.
