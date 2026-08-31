#!/usr/bin/env bash
# One-shot deploy of the latest code on the EC2:
#   pull main -> reinstall deps only if requirements changed -> restart the app
#   and dashboard -> print health. The Cloudflare tunnel is intentionally NOT
#   restarted, so the public URL stays the same across code updates.
#
#   bash ~/zendesk-agent/deploy/update.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8000}"
DASH_PORT="${DASH_PORT:-8501}"
cd "$APP_DIR"

echo "==> Pulling latest (origin/main)"
before="$(sha1sum requirements.txt 2>/dev/null | awk '{print $1}')"
git fetch -q origin
git reset --hard origin/main
after="$(sha1sum requirements.txt 2>/dev/null | awk '{print $1}')"
echo "   now at: $(git log --oneline -1)"

if [ "$before" != "$after" ]; then
  echo "==> requirements.txt changed -> installing deps"
  "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
else
  echo "==> deps unchanged (skipping pip install)"
fi

echo "==> Restarting app + dashboard (tunnel left running, URL unchanged)"
for svc in zendesk-agent zendesk-dashboard; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
    sudo systemctl restart "$svc" && echo "   restarted $svc"
  else
    echo "   ($svc not installed - skipping)"
  fi
done

echo "==> Health"
sleep 4
echo "   app:       $(curl -fsS http://127.0.0.1:$PORT/health 2>/dev/null || echo DOWN)"
echo "   sink:      $(curl -fsS http://127.0.0.1:$PORT/sink/status 2>/dev/null || echo n/a)"
echo "   dashboard: HTTP $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$DASH_PORT 2>/dev/null || echo n/a)"

echo "==> Public tunnel URL (unchanged - tunnel was not restarted):"
sudo journalctl -u cloudflared-quick -n 80 --no-pager 2>/dev/null \
  | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 \
  || echo "   (not found; check: sudo journalctl -u cloudflared-quick -f)"

echo "Done."
