#!/bin/bash
# Circular Campus - macOS setup (double-click in Finder, or run ./setup.command in Terminal)
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3; do if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi; done
if [ -z "$PY" ]; then echo "Python 3.13 not found. Install it from python.org, then run again."; exit 1; fi
"$PY" "scripts/bootstrap.py" setup "$@" || { echo "Setup FAILED - read the message above."; exit 1; }
echo "Setup finished. Next: ./run.command"
