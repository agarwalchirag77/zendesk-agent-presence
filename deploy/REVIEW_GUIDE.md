# Reviewing the Zendesk Agent Presence service

A 5-minute guide to check the service is healthy and to review its data in
Snowflake. No code changes — read-only checks.

**What it is:** a webhook server (FastAPI on an EC2 box) that ingests Zendesk
agent state + work-item events into a local SQLite DB and continuously mirrors
the append-only audit logs to **Snowflake** for durable storage / reporting.
Public ingress is a Cloudflare quick tunnel.

- Host: EC2, app dir `~/zendesk-agent`, app on `127.0.0.1:8000`
- Services (systemd): `zendesk-agent` (app), `cloudflared-quick` (tunnel)
- Snowflake: tables `STATE_EVENTS`, `WORK_ITEM_EVENTS`, `AGENTS` + `V_*` views

---

## 1. Is the app running? (SSH to the EC2)

```bash
sudo systemctl status zendesk-agent cloudflared-quick   # both should be "active (running)"
curl -s http://127.0.0.1:8000/health                    # -> {"status":"ok"}
journalctl -u zendesk-agent -n 50 --no-pager            # recent app logs / errors
```

Get the current public URL (it changes when the tunnel restarts):
```bash
sudo journalctl -u cloudflared-quick -n 80 --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
```
The Zendesk webhook should point at `https://<that-url>/webhooks/zendesk/agent-state`.

---

## 2. Is data flowing, and is Snowflake syncing?

**Sink status** (the key check) — enabled, backlog, last flush/error:
```bash
curl -s http://127.0.0.1:8000/sink/status
```
- `enabled: true` → Snowflake creds are loaded.
- `unsynced.* : 0` → everything is in Snowflake. Growing numbers = flushing is stuck.
- `last_error: null` → healthy. Non-null = creds/role/network (rows stay buffered locally, no loss).

**Local ingestion freshness** (are new events arriving at all):
```bash
cd ~/zendesk-agent
venv/bin/python - <<'PY'
import sqlite3, datetime
c = sqlite3.connect("data/presence.db")
print("now (UTC):", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
print("latest state_event:", c.execute("SELECT MAX(received_at) FROM state_events").fetchone()[0])
print("counts:", {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("state_events","work_item_events","agent_sessions","agents")})
PY
```
Latest event a few minutes old during working hours = flowing. Hours/days old while agents are active = check the tunnel URL / Zendesk webhook.

**Watch requests live** (optional):
```bash
tail -f ~/zendesk-agent/logs/app.log 2>/dev/null | grep --line-buffered POST   # only on the rootless box
journalctl -u zendesk-agent -f | grep POST                                      # on the systemd/EC2 box
```

---

## 3. Review the data in Snowflake

Open Snowsight → **Workspaces** (or a worksheet), set context, then query:
```sql
USE ROLE <role>; USE WAREHOUSE <wh>; USE SCHEMA <db>.<schema>;
```

**Is Snowflake current?**
```sql
SELECT COUNT(*) AS state_events, MAX(RECEIVED_AT) AS latest_received FROM STATE_EVENTS;
SELECT COUNT(*) FROM WORK_ITEM_EVENTS;
SELECT COUNT(*) FROM AGENTS;
```
`latest_received` should track "now" (UTC) within a minute or two of live activity.

**Reporting views** (create once from `deploy/snowflake_dashboard.sql` Part 1 if missing):
```sql
-- who is online right now (IST)
SELECT AGENT_NAME, CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC) AS ONLINE_SINCE_IST,
       ROUND(DATEDIFF('second', LOGIN_UTC, SYSDATE())/3600.0, 2) AS ONLINE_HOURS
FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL ORDER BY LOGIN_UTC;

-- online hours per agent, last 7 IST days
SELECT AGENT_NAME, ROUND(SUM(ONLINE_SECS)/3600.0,1) AS ONLINE_HOURS
FROM V_SESSIONS
WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)
      >= DATEADD('day',-7, CONVERT_TIMEZONE('UTC','Asia/Kolkata', SYSDATE()))
GROUP BY AGENT_NAME ORDER BY ONLINE_HOURS DESC;

-- latest state per agent
SELECT AGENT_NAME, CURRENT_STATE, CONVERT_TIMEZONE('UTC','Asia/Kolkata', SINCE_UTC) AS SINCE_IST
FROM V_CURRENT_STATUS ORDER BY AGENT_NAME;
```

**Dashboard:** the Streamlit-in-Snowflake app (`deploy/streamlit_app.py`) renders
all of this — Snowsight → Projects → Streamlit → open the app. (Legacy Snowsight
dashboards are being retired; Streamlit is the replacement.)

---

## 4. Quick health checklist

| Check | Healthy | If not… |
|---|---|---|
| `systemctl status` | both active (running) | `sudo systemctl restart zendesk-agent cloudflared-quick`; see `journalctl` |
| `/health` | `{"status":"ok"}` | app down → restart, check logs |
| `/sink/status` `enabled` | `true` | `.env` not loaded → check `~/zendesk-agent/.env`, restart |
| `/sink/status` `unsynced` | `0` (or shrinking) | growing → see `last_error` (creds/role/network); data is safe locally meanwhile |
| Latest `received_at` | ~now during work hours | stale → tunnel URL changed? re-point Zendesk webhook |
| Snowflake `MAX(RECEIVED_AT)` | ~now | lagging → sink stuck (see `/sink/status`) |

**Most common issue:** the quick-tunnel **URL changed** (after a restart/reboot),
so Zendesk is posting to a dead URL. Fix = re-fetch the URL (section 1) and update
the Zendesk webhook. No Snowflake data is lost during an outage — SQLite buffers
and flushes on recovery.

## Maintaining the shift roster
Shifts rotate monthly and are edited **in the dashboard → Roster tab** (stored in
Snowflake `AGENT_ROSTER`). Each month: open Roster, pick the month, "Copy previous
month" (or "Load built-in template"), adjust the rotations, **Save**. The
Shift-compliance tab reads this per selected date, so late-login / early-logout /
mid-shift-offline are evaluated against the correct current shift.
