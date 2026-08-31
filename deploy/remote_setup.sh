#!/usr/bin/env bash
# ROOTLESS setup — no sudo, nothing installed system-wide. Run on the remote
# machine as your normal user, AFTER extracting the code tarball, e.g.:
#
#   mkdir -p ~/zendesk-agent
#   tar -xzf /tmp/zendesk-agent-code.tgz -C ~/zendesk-agent
#   bash ~/zendesk-agent/deploy/remote_setup.sh
#
# Installs a venv + cloudflared binary inside the app dir, restores the DB
# snapshot from /tmp, starts the supervisor (deploy/run.sh), and adds a
# @reboot crontab entry so it comes back after a reboot. Idempotent.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8000}"
echo "==> App dir: $APP_DIR  (port $PORT)"

command -v python3 >/dev/null || { echo "python3 not found — ask your admin to install it once."; exit 1; }

echo "==> Python venv (rootless)"
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "    python $PYVER"
rm -rf "$APP_DIR/venv"   # clear any partial venv from a previous run
if python3 -m venv "$APP_DIR/venv" >/dev/null 2>&1 && [ -x "$APP_DIR/venv/bin/pip" ]; then
  echo "    created venv with pip"
else
  # Debian/Ubuntu without python3-venv: the venv module is stdlib, only
  # ensurepip is missing. Create without pip, then bootstrap pip ourselves.
  echo "    ensurepip missing -> venv --without-pip + get-pip.py"
  rm -rf "$APP_DIR/venv"
  python3 -m venv --without-pip "$APP_DIR/venv"
  curl -fsSL "https://bootstrap.pypa.io/pip/$PYVER/get-pip.py" -o /tmp/get-pip.py \
    || curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o /tmp/get-pip.py
  "$APP_DIR/venv/bin/python" /tmp/get-pip.py
fi
[ -x "$APP_DIR/venv/bin/pip" ] || { echo "ERROR: could not bootstrap pip into the venv"; exit 1; }
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> cloudflared (static binary in app dir, no root)"
mkdir -p "$APP_DIR/bin"
if [ ! -x "$APP_DIR/bin/cloudflared" ]; then
  case "$(uname -m)" in
    x86_64)        CFARCH=amd64 ;;
    aarch64|arm64) CFARCH=arm64 ;;
    armv7l|armv6l) CFARCH=arm ;;
    *) echo "    unsupported arch $(uname -m)"; exit 1 ;;
  esac
  curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CFARCH" \
    -o "$APP_DIR/bin/cloudflared"
  chmod +x "$APP_DIR/bin/cloudflared"
fi
"$APP_DIR/bin/cloudflared" --version

echo "==> Restore DB snapshot (if provided and not already present)"
mkdir -p "$APP_DIR/data"
SNAP="${DB_SNAPSHOT:-}"
if [ -z "$SNAP" ]; then
  for c in /tmp/presence.db "$HOME/tmp/presence.db" "$HOME/presence.db" ./presence.db; do
    [ -f "$c" ] && { SNAP="$c"; break; }
  done
fi
if [ -n "$SNAP" ] && [ ! -f "$APP_DIR/data/presence.db" ]; then
  cp "$SNAP" "$APP_DIR/data/presence.db"
  echo "    restored $SNAP -> $APP_DIR/data/presence.db"
else
  echo "    using existing/empty DB (override with: DB_SNAPSHOT=/path/presence.db)"
fi

chmod +x "$APP_DIR/deploy/run.sh" "$APP_DIR/deploy/stop.sh"

echo "==> Starting supervisor (detached, survives logout)"
nohup setsid env PORT="$PORT" bash "$APP_DIR/deploy/run.sh" >/dev/null 2>&1 &
sleep 4

echo "==> Auto-start on reboot (user crontab, no root)"
if command -v crontab >/dev/null 2>&1; then
  CRON_LINE="@reboot PORT=$PORT bash $APP_DIR/deploy/run.sh >/dev/null 2>&1"
  # Tolerant of an empty/absent crontab: grep over empty input returns 1, which
  # would otherwise abort the script under `set -euo pipefail`.
  existing="$(crontab -l 2>/dev/null || true)"
  { printf '%s\n' "$existing" | grep -vF "$APP_DIR/deploy/run.sh" || true; echo "$CRON_LINE"; } | crontab -
  echo "    added @reboot entry"
else
  echo "    'crontab' not available — reboot auto-start skipped (see DEPLOY.md alternatives)"
fi

echo "==> Health: $(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || echo 'still starting...')"
echo "==> Migrated row counts:"
"$APP_DIR/venv/bin/python" - "$APP_DIR/data/presence.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for t in ("agents", "state_events", "work_items"):
    try: print("   ", t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    except Exception: print("   ", t, "n/a")
PY
true  # don't let a non-zero from the heredoc abort the script under set -e

echo "==> Public URL (changes on every tunnel restart):"
sleep 5
cat "$APP_DIR/logs/public-url.txt" 2>/dev/null \
  || grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$APP_DIR/logs/tunnel.log" 2>/dev/null | tail -1 \
  || echo "    not ready yet — run: cat $APP_DIR/logs/public-url.txt"

echo
echo "Done. Append /webhooks/zendesk/agent-state to that URL in Zendesk."
echo "Logs: $APP_DIR/logs/  |  Stop: bash $APP_DIR/deploy/stop.sh"
