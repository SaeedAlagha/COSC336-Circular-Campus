#!/bin/bash
# Circular Campus - macOS run. Open http://127.0.0.1:8000/ ; press Ctrl+C to stop.
cd "$(dirname "$0")" || exit 1
".venv/bin/python" "scripts/bootstrap.py" run "$@"
