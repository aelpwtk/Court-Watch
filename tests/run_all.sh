#!/bin/sh
# Runs every test; exits non-zero if any fails. Usage: sh tests/run_all.sh
set -e
for t in "$(dirname "$0")"/test_*.py; do echo "== $t"; python3 "$t" | tail -1; done
echo "ALL TESTS PASSED"
