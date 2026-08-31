#!/usr/bin/env bash
# Install the self-hosted Streamlit dashboard as a systemd service on the EC2.
# Serves on 0.0.0.0:8501 so it's browser-reachable (no SSH/pem needed).
# Run as your normal user (it calls sudo for the unit + systemctl):
#
#   bash ~/zendesk-agent/deploy/dashboard_setup.sh
#
# Reads Snowflake creds + DASHBOARD_PASSWORD from ~/zendesk-agent/.env.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
PORT="${DASH_PORT:-8501}"
echo "==> App dir: $APP_DIR   user: $RUN_USER   dashboard port: $PORT"

echo "==> Ensure deps (streamlit, snowflake-connector) in the venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> systemd unit: zendesk-dashboard"
sudo tee /etc/systemd/system/zendesk-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Zendesk Agent Presence Dashboard (Streamlit)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/streamlit run $APP_DIR/dashboard/dashboard.py \
  --server.address 0.0.0.0 --server.port $PORT \
  --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now zendesk-dashboard
sleep 4
echo "==> Status"
systemctl --no-pager --lines=5 status zendesk-dashboard || true

echo
echo "Dashboard should be at:  http://<EC2-PUBLIC-DNS-OR-IP>:$PORT"
echo
echo "IMPORTANT — open the port to your network in the EC2 Security Group:"
echo "  add inbound rule: Custom TCP, port $PORT, source = your office/VPN CIDR"
echo "  (use 0.0.0.0/0 only if you accept public exposure; keep DASHBOARD_PASSWORD set)"
echo "Manage: sudo systemctl restart zendesk-dashboard  |  journalctl -u zendesk-dashboard -f"
