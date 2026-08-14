#!/usr/bin/env bash
# Starts the stock app on macOS or Linux. The Windows equivalent is run.ps1.
# First run creates a virtual environment and installs the dependencies.
#
#   ./run.sh              listen on the network, port 8000
#   PORT=8080 ./run.sh    different port
#   LOCAL_ONLY=1 ./run.sh only this machine can reach it

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
if [ -n "${LOCAL_ONLY:-}" ]; then LISTEN="127.0.0.1"; else LISTEN="0.0.0.0"; fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Setting up the virtual environment (one-off, takes a minute)..."
    python3 -m venv .venv
    ./.venv/bin/python -m pip install --upgrade pip --quiet
    ./.venv/bin/python -m pip install -r requirements.txt
    echo "Done."
fi

echo
echo "IT stock app starting."
echo "  On this machine:  http://localhost:${PORT}"
if [ "$LISTEN" = "0.0.0.0" ]; then
    # Best effort at the LAN address, so colleagues have something to type.
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    [ -z "$ip" ] && ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -n "$ip" ] && echo "  On the network:   http://${ip}:${PORT}"
fi
echo "  Stop with Ctrl+C."
echo

exec ./.venv/bin/python -m uvicorn app.main:app --host "$LISTEN" --port "$PORT"
