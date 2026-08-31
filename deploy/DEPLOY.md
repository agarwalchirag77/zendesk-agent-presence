# Deploying to a remote Ubuntu machine — ROOTLESS (no sudo)

Everything runs under your normal user: a Python venv and the `cloudflared`
binary live inside the app dir, and a small **watchdog script** keeps the app +
Cloudflare quick tunnel alive (restart-on-crash, survives logout, and restarts
on reboot via your personal crontab). Nothing is installed system-wide; root is
never touched. Your current DB data is migrated along the way.

> **Quick-tunnel caveat:** the `*.trycloudflare.com` URL **changes whenever the
> tunnel restarts** (crash or reboot). Re-read it from `logs/public-url.txt`
> and update Zendesk's webhook when it changes.

Prereq on the remote: **python3** must already exist (it does on stock Ubuntu).
Everything else is set up rootlessly. `cron` is normally running on Ubuntu — if
it isn't, see "Reboot persistence" below.

---

## 1. On your LOCAL Mac — package code + DB snapshot

```bash
cd "/Users/hevo/Documents/Support tools/Agent Audit logging"
bash deploy/package.sh
# -> /tmp/zendesk-agent-code.tgz  and  /tmp/presence.db

scp /tmp/zendesk-agent-code.tgz /tmp/presence.db ubuntu@REMOTE_IP:/tmp/
```

The snapshot uses SQLite's backup API, so it folds in the WAL (your live
`presence.db` is tiny because most rows are still in `presence.db-wal`).

---

## 2. On the REMOTE machine — extract + run setup (as your normal user)

```bash
mkdir -p ~/zendesk-agent
tar -xzf /tmp/zendesk-agent-code.tgz -C ~/zendesk-agent
bash ~/zendesk-agent/deploy/remote_setup.sh
```

`remote_setup.sh` (no sudo) builds the venv, downloads the `cloudflared` binary
into `~/zendesk-agent/bin`, restores the DB, starts the supervisor, adds the
`@reboot` crontab entry, and prints the health check, row counts, and public URL.

Pick a different port if 8000 is taken: `PORT=8123 bash ~/zendesk-agent/deploy/remote_setup.sh`.

---

## 3. Point Zendesk at the public URL

```bash
cat ~/zendesk-agent/logs/public-url.txt        # the trycloudflare URL
```
Use `<that URL>/webhooks/zendesk/agent-state` in Zendesk. Re-check after any
restart, since it changes.

---

## Operating it (all rootless)

```bash
# logs
tail -f ~/zendesk-agent/logs/app.log
tail -f ~/zendesk-agent/logs/tunnel.log
cat   ~/zendesk-agent/logs/run.log            # supervisor restarts

# stop / start
bash ~/zendesk-agent/deploy/stop.sh
nohup setsid bash ~/zendesk-agent/deploy/run.sh >/dev/null 2>&1 &

# is it up?
curl http://127.0.0.1:8000/health

# sync agent directory (names in reports)
ZENDESK_SUBDOMAIN=xxx ZENDESK_EMAIL=you@co.com ZENDESK_API_TOKEN=xxx \
  ~/zendesk-agent/venv/bin/python ~/zendesk-agent/scripts/sync_agents.py

# query reports locally
curl "http://127.0.0.1:8000/reports/daily-status"
curl "http://127.0.0.1:8000/reports/daily-workload"

# back up the DB (python, no CLI)
~/zendesk-agent/venv/bin/python - <<'PY'
import sqlite3, datetime
s=sqlite3.connect("/home/USER/zendesk-agent/data/presence.db")
d=sqlite3.connect(f"/home/USER/zendesk-agent/data/backup-{datetime.date.today()}.db")
with d: s.backup(d)
PY
```

## Updating code later
Re-run `deploy/package.sh` locally, `scp` the tarball, then on the remote:
```bash
tar -xzf /tmp/zendesk-agent-code.tgz -C ~/zendesk-agent
bash ~/zendesk-agent/deploy/stop.sh
nohup setsid bash ~/zendesk-agent/deploy/run.sh >/dev/null 2>&1 &
```
The tarball never includes `data/`, so your DB is safe across updates.

## Reboot persistence
- **Crash & logout** are always covered: the watchdog restarts crashed
  processes, and `nohup setsid` keeps it running after you disconnect.
- **Reboot** uses the `@reboot` crontab entry (added automatically). This needs
  the `cron` daemon running — standard on Ubuntu. If `crontab` isn't available,
  ask your admin once to either install cron or run `loginctl enable-linger
  <user>`; with linger you can instead use a **systemd *user* service**
  (`~/.config/systemd/user/`), no root required.

## Want a permanent URL later?
A named Cloudflare tunnel gives a stable `https://...yourdomain.com` but needs a
Cloudflare account + a domain (and `cloudflared tunnel login`). It still runs
rootlessly. Ask and I'll provide the rootless named-tunnel steps.
