#!/usr/bin/env bash
# Run on the LOCAL (Mac) machine. Produces two artifacts in /tmp:
#   /tmp/zendesk-agent-code.tgz  -> code + deploy files (no venv, no live DB)
#   /tmp/presence.db             -> a consistent snapshot of the current DB
# Then scp both to the remote machine (see DEPLOY.md).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Project: $PROJECT_DIR"

# 1) Consistent DB snapshot via Python's sqlite3 backup API (no sqlite3 CLI
#    needed). This folds in the WAL, so we capture ALL rows even though most
#    live data may still be sitting in presence.db-wal.
if [ -f data/presence.db ]; then
  python3 - data/presence.db /tmp/presence.db <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)
s.close(); d.close()
PY
  echo "DB snapshot -> /tmp/presence.db ($(du -h /tmp/presence.db | cut -f1))"
else
  echo "No data/presence.db found; deploying with an empty DB."
  rm -f /tmp/presence.db
fi

# 2) Code tarball (exclude venv, caches, and the live data dir).
tar -czf /tmp/zendesk-agent-code.tgz \
  --exclude='__pycache__' --exclude='*.pyc' \
  app scripts deploy dashboard requirements.txt README.md .gitignore .env.example
echo "Code tarball -> /tmp/zendesk-agent-code.tgz ($(du -h /tmp/zendesk-agent-code.tgz | cut -f1))"

echo
echo "Next: copy both to the remote, e.g."
echo "  scp /tmp/zendesk-agent-code.tgz /tmp/presence.db <user>@<remote-ip>:/tmp/"
