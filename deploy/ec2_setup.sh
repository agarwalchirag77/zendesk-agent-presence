#!/usr/bin/env bash
# EC2 / sudo deployment: installs the app + a Cloudflare quick tunnel as
# systemd services (proper auto-restart + reboot survival). The tunnel makes
# only OUTBOUND connections, so no inbound security-group ports are needed.
#
# Run as your normal login user (NOT with sudo) — the script calls sudo itself
# only for apt, the unit files, and systemctl:
#
#   tar -xzf /tmp/zendesk-agent-code.tgz -C ~/zendesk-agent
#   bash ~/zendesk-agent/deploy/ec2_setup.sh
#
# Starts with a FRESH database (created on first run). Idempotent.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
PORT="${PORT:-8000}"
echo "==> App dir: $APP_DIR   user: $RUN_USER   port: $PORT"

echo "==> System packages (sudo)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip curl

echo "==> Python venv + deps (as $RUN_USER)"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> cloudflared binary"
mkdir -p "$APP_DIR/bin"
if [ ! -x "$APP_DIR/bin/cloudflared" ]; then
  case "$(uname -m)" in
    x86_64)        A=amd64 ;;
    aarch64|arm64) A=arm64 ;;
    *) echo "unsupported arch $(uname -m)"; exit 1 ;;
  esac
  curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A" \
    -o "$APP_DIR/bin/cloudflared"
  chmod +x "$APP_DIR/bin/cloudflared"
fi
"$APP_DIR/bin/cloudflared" --version

echo "==> systemd unit: zendesk-agent (app)"
sudo tee /etc/systemd/system/zendesk-agent.service >/dev/null <<UNIT
[Unit]
Description=Zendesk Agent Presence Tracker (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
# Optional secrets/config (Snowflake creds, ZENDESK_WEBHOOK_SECRET, ...).
# Leading "-" => fine if the file is absent. Create it chmod 600 (see below).
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

echo "==> systemd unit: cloudflared-quick (tunnel)"
sudo tee /etc/systemd/system/cloudflared-quick.service >/dev/null <<UNIT
[Unit]
Description=Cloudflare Quick Tunnel for Zendesk Agent Tracker
After=network-online.target zendesk-agent.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
ExecStart=$APP_DIR/bin/cloudflared tunnel --protocol http2 --no-autoupdate --url http://localhost:$PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Enable + start services"
sudo systemctl daemon-reload
sudo systemctl enable --now zendesk-agent cloudflared-quick

echo "==> Health"
sleep 5
curl -fsS "http://127.0.0.1:$PORT/health" && echo || echo "app still starting; check: journalctl -u zendesk-agent -e"

echo "==> Public tunnel URL (from journald):"
sleep 4
sudo journalctl -u cloudflared-quick -n 80 --no-pager \
  | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 \
  || echo "not ready yet — run: sudo journalctl -u cloudflared-quick -f | grep trycloudflare"

echo
echo "Done. Append /webhooks/zendesk/agent-state to that URL in Zendesk."
echo "Manage: sudo systemctl status|restart zendesk-agent cloudflared-quick"
echo "Logs:   journalctl -u zendesk-agent -f   |   journalctl -u cloudflared-quick -f"
echo
echo "Snowflake sink (optional): create $APP_DIR/.env (chmod 600) with:"
echo "  SNOWFLAKE_ACCOUNT=...  SNOWFLAKE_USER=...  SNOWFLAKE_PASSWORD=..."
echo "  SNOWFLAKE_WAREHOUSE=...  SNOWFLAKE_DATABASE=...  SNOWFLAKE_SCHEMA=...  (SNOWFLAKE_ROLE=... optional)"
echo "then: sudo systemctl restart zendesk-agent  &&  curl localhost:$PORT/sink/status"
