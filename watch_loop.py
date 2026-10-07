#!/usr/bin/env python3
"""Runs the court checks (Challenge, Ace, Crystal) once a minute for the length of a GitHub run.
GitHub only starts a run every 15-20 min, so one check per run leaves big blind
spots; looping inside the run closes them. Runs are queued back to back by the
workflow, so coverage is close to continuous. State files persist between
iterations on disk and are committed once at the end of the run."""

import os
import time

import check_ace
import check_courts

try:                      # Crystal is the newest bot: if it ever breaks, the two clubs keep running
    import check_crystal
except Exception as _e:   # pragma: no cover
    check_crystal = None
    print(f"[Crystal] could not load: {_e}")

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", 13 * 60))
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", 60))


def main():
    end = time.monotonic() + LOOP_SECONDS
    n = 0
    while True:
        n += 1
        t0 = time.monotonic()
        # Crystal goes last so a slow site can never delay the two clubs that matter most
        bots = [("Challenge", check_courts.main), ("Ace", check_ace.main)]
        if check_crystal:
            bots.append(("Crystal", check_crystal.main))
        for name, fn in bots:
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
