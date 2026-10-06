#!/usr/bin/env python3
"""Runs both court checks once a minute for ~13 minutes per GitHub run.
GitHub only starts a run every 15-20 min, so one check per run leaves big blind
spots; looping inside the run closes them. Runs are queued back to back by the
workflow, so coverage is close to continuous. State files persist between
iterations on disk and are committed once at the end of the run."""

import os
import time

import check_ace
import check_courts

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", 13 * 60))
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", 60))


def main():
    end = time.monotonic() + LOOP_SECONDS
    n = 0
    while True:
        n += 1
        t0 = time.monotonic()
        for name, fn in (("Challenge", check_courts.main), ("Ace", check_ace.main)):
            try:
                fn()
            except Exception as e:      # one club failing must never stop the other
                print(f"[{name}] unexpected error: {e}")
        wait = INTERVAL - (time.monotonic() - t0)
        if time.monotonic() + max(wait, 0) >= end:
            break
        time.sleep(max(wait, 0))
    print(f"Done: {n} check rounds this run.")


if __name__ == "__main__":
    main()
