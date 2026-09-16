# Handover — Zendesk Agent Presence Tracker

Everything a new maintainer needs to: understand the system, run it locally, ship
changes, and keep it healthy on the remote server.

- **Repo:** https://github.com/agarwalchirag77/zendesk-agent-presence (public)
- **Related docs in this repo:** [`deploy/DEPLOY.md`](deploy/DEPLOY.md) (fresh-server
  deploy details), [`deploy/REVIEW_GUIDE.md`](deploy/REVIEW_GUIDE.md) (5-min health check),
  [`README.md`](README.md) (feature/API reference).

---

## 1. What this system does

Zendesk sends webhook **events** whenever a support agent changes availability
(online/away/offline) or is assigned/handed a work item. This app ingests those
events, tracks **who was online and until when**, and powers **shift-compliance
reporting** (late login / early logout / mid-shift offline) plus a live dashboard.

```
Zendesk ──HTTPS webhook──► Cloudflare quick tunnel ──► FastAPI app (EC2, :8000)
                                                          │  writes
                                                          ▼
                                                   SQLite  (data/presence.db)   ← hot store + outbox
                                                          │  background flusher (sink)
                                                          ▼
                                                   Snowflake  (STATE_EVENTS, WORK_ITEM_EVENTS, AGENTS)
                                                          │  V_* views + AGENT_ROSTER
                                                          ▼
                                                   Streamlit dashboard (EC2, :8501, browser)
```

Key design choices (so nothing surprises you):
- **SQLite is the hot store AND a durable outbox.** Events land locally instantly;
  a background thread mirrors the append-only audit logs to Snowflake and marks
  rows `sf_synced_at`. If Snowflake is down, rows buffer locally and flush later —
  no data loss. Snowflake is the long-term/BI store and survives EC2 loss.
- **All reporting is IST** (Asia/Kolkata). Storage is UTC; IST is applied at read.
- **"online" = any non-offline state.** A session stays open across
  online→away→online and only closes on `offline`. Disconnected-but-not-offline is
  still counted as online (that was a deliberate business decision).
- **Ingress is a Cloudflare *quick* tunnel** — free, no domain, but the public URL
  **changes whenever the tunnel restarts** (see §7 gotchas).

---

## 2. Repo layout

```
app/                      FastAPI webhook server (the ingest side)
  main.py                 endpoints: /webhooks/..., /reports/..., /health, /sink/status
  events.py               parse + handle presence & work-item events
  db.py                   SQLite schema + connection (WAL) + outbox column migration
  timeutil.py             IST helpers
  snowflake_sink.py       background flusher: mirrors audit logs to Snowflake
dashboard/
  dashboard.py            Streamlit dashboard (reads Snowflake) — the report UI
scripts/
  sync_agents.py          pull the Zendesk agent directory into the AGENTS table
  snowflake_init.py       verify Snowflake creds + create tables
  send_sample_event.py    replay sample events for local testing
  shift_report.sql        legacy SQLite shift-compliance query (superseded by dashboard)
deploy/
  ec2_setup.sh            first-time server setup (systemd services for app + tunnel)
  dashboard_setup.sh      first-time dashboard systemd service (:8501)
  update.sh               one-shot deploy: git pull → pip (if changed) → restart
  run.sh / stop.sh        rootless supervisor (only for a no-sudo box; EC2 uses systemd)
  snowflake_dashboard.sql Snowflake views (V_*) + AGENT_ROSTER DDL — run once in Snowsight
  DEPLOY.md / REVIEW_GUIDE.md
requirements.txt          fastapi, uvicorn, snowflake-connector-python, streamlit, certifi
.env.example              template for the server .env (never commit a real .env)
```

`.gitignore` excludes `venv/`, `data/` (the SQLite DB), and `.env` — those never
go in git.

---

## 3. Run it locally (for making changes)

```bash
git clone https://github.com/agarwalchirag77/zendesk-agent-presence.git
cd zendesk-agent-presence
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**The ingest app (no Snowflake needed):**
```bash
uvicorn app.main:app --reload --port 8000
# in another shell, replay sample events and hit the reports:
python scripts/send_sample_event.py
curl "http://localhost:8000/reports/daily-status"
```
Without `SNOWFLAKE_*` env vars the sink is disabled and it runs SQLite-only —
perfect for developing ingest/report logic.

**The dashboard (needs Snowflake creds to show anything):**
```bash
export SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=... \
       SNOWFLAKE_WAREHOUSE=... SNOWFLAKE_DATABASE=... SNOWFLAKE_SCHEMA=...
streamlit run dashboard/dashboard.py      # opens http://localhost:8501
```
Get the Snowflake values from the server's `.env` (see §6) or your infra/data team.

**Change → ship loop:** edit code → test locally → commit → push to `main` →
deploy on the server with `update.sh` (§5). The server is a plain git checkout,
so deploys are just `git pull`.

---

## 4. Configuration (env vars)

Set on the **server** in `~/zendesk-agent/.env` (chmod 600, gitignored), loaded by
systemd via `EnvironmentFile`. Template: [`.env.example`](.env.example).

| Var | Needed for | Notes |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` `_USER` `_PASSWORD` | sink + dashboard | all required to enable Snowflake |
| `SNOWFLAKE_WAREHOUSE` `_DATABASE` `_SCHEMA` | sink + dashboard | where tables/views live |
| `SNOWFLAKE_ROLE` | optional | role override |
| `DASHBOARD_PASSWORD` | dashboard | gates the browser page (set it — the port is internet-adjacent) |
| `ZENDESK_WEBHOOK_SECRET` | optional | if set, the webhook verifies an HMAC signature |
| `ZENDESK_SUBDOMAIN` `_EMAIL` `_API_TOKEN` | `scripts/sync_agents.py` | to refresh the agent directory |

Secrets live only in the server `.env` — **never** in git or chat.

---

## 5. Deploying changes to the remote server

The EC2 box has the repo cloned at `~/zendesk-agent` and runs three **systemd**
services. Normal deploy after you push to `main`:

```bash
ssh <ec2-user>@<ec2-host>          # needs the .pem key
bash ~/zendesk-agent/deploy/update.sh
```
`update.sh` does: `git reset --hard origin/main` → `pip install` **only if
requirements changed** → restart the **app + dashboard** (the tunnel is left
running so the public URL doesn't change) → prints health + the tunnel URL.
`.env` and `data/` are gitignored, so they're never touched by a pull.

**Services (systemd):**
| Service | What | Port |
|---|---|---|
| `zendesk-agent` | FastAPI ingest app (uvicorn) | 127.0.0.1:8000 |
| `cloudflared-quick` | Cloudflare tunnel → public URL | (outbound only) |
| `zendesk-dashboard` | Streamlit dashboard | 0.0.0.0:8501 |

**Setting up a brand-new server** (or if `~/zendesk-agent` isn't a git checkout yet):
see [`deploy/DEPLOY.md`](deploy/DEPLOY.md). In short:
```bash
git clone https://github.com/agarwalchirag77/zendesk-agent-presence.git ~/zendesk-agent
cp ~/zendesk-agent/.env.example ~/zendesk-agent/.env && nano ~/zendesk-agent/.env   # fill secrets
bash ~/zendesk-agent/deploy/ec2_setup.sh          # app + tunnel systemd services
bash ~/zendesk-agent/deploy/dashboard_setup.sh    # dashboard systemd service
python ~/zendesk-agent/scripts/snowflake_init.py  # verify Snowflake + create tables
```
Then run **Part 1** of [`deploy/snowflake_dashboard.sql`](deploy/snowflake_dashboard.sql)
once in Snowsight to create the `V_*` views, open the tunnel URL, and point the
Zendesk webhook at `https://<tunnel>/webhooks/zendesk/agent-state`.

**Zendesk webhook events to subscribe to:** *Agent unified state changed* (presence),
and *Agent work item added* / *removed* (workload); *updated* is optional. The
webhook path is `/webhooks/zendesk/agent-state`.

---

## 6. Maintaining it on the server

```bash
# status + logs
sudo systemctl status zendesk-agent cloudflared-quick zendesk-dashboard
journalctl -u zendesk-agent -f            # app + sink logs
journalctl -u zendesk-dashboard -f        # dashboard logs

# health
curl -s http://127.0.0.1:8000/health      # {"status":"ok"}
curl -s http://127.0.0.1:8000/sink/status # enabled?, unsynced backlog, last_error
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501   # dashboard up?

# current public webhook URL (changes on tunnel restart)
sudo journalctl -u cloudflared-quick -n 80 --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1

# refresh the agent directory (names in reports/dashboard)
ZENDESK_SUBDOMAIN=.. ZENDESK_EMAIL=.. ZENDESK_API_TOKEN=.. \
  ~/zendesk-agent/venv/bin/python ~/zendesk-agent/scripts/sync_agents.py

# restart everything
sudo systemctl restart zendesk-agent cloudflared-quick zendesk-dashboard
```

`deploy/REVIEW_GUIDE.md` is the quick "is it healthy?" checklist for non-devs.

---

## 7. Common issues & gotchas

- **Zendesk stopped delivering / data went stale.** The quick-tunnel **URL
  changed** (after a tunnel restart or reboot). Re-fetch it (§6) and update the
  Zendesk webhook. No data is lost during the gap. *Permanent fix:* switch to a
  named Cloudflare tunnel with your own domain (see DEPLOY.md).
- **`/sink/status` shows a `last_error` / `unsynced` climbing.** Snowflake creds/
  role/network problem — rows are safe in SQLite and flush once fixed. Check the
  error, fix `.env`, `systemctl restart zendesk-agent`.
- **Dashboard: "Authentication token has expired" (390114).** Handled now
  (keep-alive + auto-reconnect); if it persists it's a genuine creds/network issue.
- **Dashboard shows wrong/absent shift compliance.** Roster coverage — each roster
  has an explicit `[start, end]` range in the **Roster tab**; a date must fall in a
  range. Delete stray periods and set the correct start/end (Roster tab has Delete
  + end-date picker).
- **SQLite WAL.** `data/presence.db` may look tiny because recent writes sit in
  `presence.db-wal` until checkpoint. To copy/back up the DB use
  `sqlite3 .backup` (or the Python `.backup` in `deploy/package.sh`), never a raw
  copy of the `.db` file alone.
- **Snowflake cost.** Point the sink + dashboard at a small (XS) warehouse with a
  short `AUTO_SUSPEND`; a dashboard left open holds the warehouse warm.

---

## 8. Snowflake schema (in your DATABASE.SCHEMA)

**Base tables** (written by the sink; all `VARCHAR`, timestamps kept as raw ISO):
- `STATE_EVENTS` — presence audit log
- `WORK_ITEM_EVENTS` — work-item audit log
- `AGENTS` — directory (`AGENT_ID, NAME, EMAIL, ROLE, ACTIVE, SYNCED_AT`)

**Views** (from `deploy/snowflake_dashboard.sql`; parse ISO→`TIMESTAMP_NTZ` UTC):
- `V_STATE_EVENTS`, `V_SESSIONS` (login/logout spans), `V_CURRENT_STATUS`,
  `V_WORK_ITEMS`

**Roster** (managed by the dashboard's Roster tab):
- `AGENT_ROSTER (PERIOD_START, PERIOD_END, AGENT_ID, AGENT_NAME, LEVEL, DOW,
  SHIFT_CODE, CUSTOM_START, CUSTOM_END, UPDATED_AT)` — a roster applies to every
  date in `[PERIOD_START, PERIOD_END]`. Shift codes: M/A/N/D/E/CUSTOM (see
  `SHIFTS` in `dashboard.py` for the IST windows). The dashboard's Snowflake role
  needs `INSERT/UPDATE/DELETE` on this table.

The sink only mirrors the **append-only audit logs + AGENTS**; sessions/work-items/
status are **derived by the views**, so if you need to change how a session is
defined, edit the views — not the ingest.

---

## 9. Access checklist for the new owner

- [ ] GitHub access to the repo (it's public to read; push access to `agarwalchirag77/zendesk-agent-presence` if you'll commit).
- [ ] SSH `.pem` key + host for the EC2 box.
- [ ] The server `~/zendesk-agent/.env` values (or the ability to regenerate: Snowflake service user, Zendesk API token).
- [ ] Snowflake access to the `DATABASE.SCHEMA` (to inspect data / run views).
- [ ] Zendesk admin access (to view/repoint the webhook and re-subscribe events).
