#!/usr/bin/env bash
# Rootless stop: terminates the supervisor, app, and tunnel for THIS install,
# and frees the port (the watchdog's background loops get orphaned on a plain
# kill and would otherwise respawn the app).
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8000}"

# -9 so the respawning watchdog loops can't restart things between signals.
pkill -9 -f "$APP_DIR/deploy/run.sh"        2>/dev/null
pkill -9 -f "$APP_DIR/venv/bin/uvicorn"     2>/dev/null
pkill -9 -f "$APP_DIR/bin/cloudflared"      2>/dev/null

# Belt-and-suspenders: free the port directly in case anything slipped through.
command -v fuser >/dev/null 2>&1 && fuser -k "${PORT}/tcp" 2>/dev/null

sleep 1
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "WARNING: something is still listening on :${PORT} (maybe another service) — check: ss -ltnp | grep :${PORT}"
else
  echo "stopped supervisor, app, and tunnel for $APP_DIR (port ${PORT} free)"
fi
echo "(to also stop auto-start on reboot, run: crontab -e  and remove the run.sh line)"
