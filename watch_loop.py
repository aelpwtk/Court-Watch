#!/usr/bin/env python3
"""Runs the court checks (Challenge) once a minute for the length of a GitHub run.
GitHub only starts a run every 15-20 min, so one check per run leaves big blind
spots; looping inside the run closes them. Runs are queued back to back by the
workflow, so coverage is close to continuous. State files persist between
iterations on disk and are committed once at the end of the run."""

import os
import time

import check_courts

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", 13 * 60))
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", 20))   # Challenge is checked this often


def main():
    end = time.monotonic() + LOOP_SECONDS
    n = 0
    while True:
        n += 1
        t0 = time.monotonic()
        # Challenge (plain public endpoint) every tick.
        bots = [("Challenge", check_courts.main)]
        for name, fn in bots:
            try:
                fn()
            except Exception as e:      # a failing check must never stop the loop
                print(f"[{name}] unexpected error: {e}")
        wait = INTERVAL - (time.monotonic() - t0)
        if time.monotonic() + max(wait, 0) >= end:
            break
        time.sleep(max(wait, 0))
    print(f"Done: {n} check rounds this run.")


if __name__ == "__main__":
    main()
