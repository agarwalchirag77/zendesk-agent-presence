#!/usr/bin/env bash
# Rootless supervisor: runs the FastAPI app and a Cloudflare quick tunnel,
# restarting either if it crashes. Single-instance (flock), detaches cleanly,
# and writes the current public URL to logs/public-url.txt.
#
# Started by remote_setup.sh (via nohup) and by the @reboot crontab entry.
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8000}"
CF_BIN="$APP_DIR/bin/cloudflared"
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$APP_DIR"   # so `uvicorn app.main:app` can import the app package

# Single-instance guard: only one supervisor at a time (manual start + @reboot
# both call this; flock makes the second one a no-op).
exec 9>"$APP_DIR/run.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') supervisor already running; exiting" >>"$LOG_DIR/run.log"
  exit 0
fi
echo "$(date '+%F %T') supervisor up (port $PORT)" >>"$LOG_DIR/run.log"

# App, with crash-restart.
(
  while true; do
    "$APP_DIR/venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" --workers 1 \
      >>"$LOG_DIR/app.log" 2>&1
    echo "$(date '+%F %T') app exited ($?); restart in 3s" >>"$LOG_DIR/run.log"
    sleep 3
  done
) &

# Cloudflare quick tunnel, with crash-restart. http2 transport avoids the
# QUIC/UDP-7844 block. NOTE: the trycloudflare URL changes on each restart.
(
  while true; do
    "$CF_BIN" tunnel --protocol http2 --no-autoupdate --url "http://localhost:$PORT" \
      >>"$LOG_DIR/tunnel.log" 2>&1
    echo "$(date '+%F %T') tunnel exited ($?); restart in 5s" >>"$LOG_DIR/run.log"
    sleep 5
  done
) &

# Keep the latest public URL handy in a file.
(
  while true; do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" 2>/dev/null | tail -1)
    [ -n "$url" ] && echo "$url" >"$LOG_DIR/public-url.txt"
    sleep 10
  done
) &

wait
