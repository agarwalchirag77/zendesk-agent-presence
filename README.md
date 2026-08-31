# Zendesk Agent Presence Tracker

A webhook server that ingests Zendesk **agent activity** events and produces two
daily reports for the support team: who was online and until when, and how much
work each agent handled.

It handles two event families on the same webhook endpoint:

**Presence** — `zen:event-type:agent.unified_state_changed`:
1. Appends to an audit log (`state_events`), idempotent on the Zendesk event `id`.
2. Updates the agent's current presence (`agent_status`).
3. Opens / closes **online sessions** (`agent_sessions`):
   - **offline → non-offline** opens a session (login),
   - **→ offline** closes it (logout),
   - intermediate states (`online → away → online`) stay within one session.

A session with `logout_at = NULL` means the agent is online right now; a closed
session's `logout_at` is exactly "online till what time".

**Work items** — `agent.work_item_added` / `_updated` / `_removed`:
1. Appends to an audit log (`work_item_events`), idempotent on event `id`.
2. Maintains a per-(agent, work-item) handling span in `work_items`:
   *added* opens a span, *updated* records reason changes (stamping `accepted_at`
   on `ACCEPTED`), *removed* closes it and computes `handle_secs`.

## Which Zendesk events to subscribe to

| For | Subscribe to |
|---|---|
| Daily status report | **Agent unified state changed** (`agent.unified_state_changed`) |
| Daily worked-on report | **Agent work item added** + **Agent work item removed** |
| Accurate handle times (offered vs accepted) | **Agent work item updated** *(recommended)* |

*"Agent per channel status changed" is **not** needed — unified state already
means "online on any channel."*

## Stack

- Python + FastAPI + Uvicorn
- SQLite via the stdlib `sqlite3` (file at `data/presence.db`, no server to run)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Interactive API docs are served at <http://localhost:8000/docs>.

In another terminal, push the bundled sample events (a presence
offline→online→offline cycle plus a work-item offered→accepted→solved cycle):

```bash
source venv/bin/activate
python scripts/send_sample_event.py
curl http://localhost:8000/agents/online
curl "http://localhost:8000/reports/daily-status?date=2023-04-06"
curl "http://localhost:8000/reports/daily-workload?date=2023-04-06"
```

## Webhook endpoint

```
POST /webhooks/zendesk/agent-state
Content-Type: application/json
```

Accepts a single event object or an array of events. Point your Zendesk
webhook at this path. The agent id is read from `detail.agent_id` and falls
back to parsing `subject` (`zen:agent:<id>`).

### Signature verification (optional)

Set `ZENDESK_WEBHOOK_SECRET` to require an HMAC-SHA256 signature of the raw
request body in the `X-Zendesk-Webhook-Signature` header (base64). If unset,
verification is skipped.

```bash
ZENDESK_WEBHOOK_SECRET=your-secret uvicorn app.main:app --port 8000
```

## Daily reports

Both reports take an optional `?date=YYYY-MM-DD` (an **IST** calendar day;
defaults to **yesterday IST**) and show agent **names** when the directory has
been synced (see below), falling back to the agent id otherwise.

| Method & path | Purpose |
|---|---|
| `GET /reports/daily-status?date=` | Per agent: `first_login_ist`, `last_logout_ist` (null if still online), `online_secs`/`online_hms`, `session_count`. |
| `GET /reports/daily-workload?date=` | Per agent: `items_added`, `items_completed`, `by_channel`, `accepted_count` vs `offered_only_count`, `total`/`avg` handle time. |

All timestamps and the day boundary are **IST (Asia/Kolkata, UTC+5:30)**;
data is stored in UTC and rendered to IST only in these reports.

### Agent names

```bash
export ZENDESK_SUBDOMAIN=yourcompany
export ZENDESK_EMAIL=you@company.com
export ZENDESK_API_TOKEN=xxxxxxxx
python scripts/sync_agents.py        # fills the `agents` table; rerun when roster changes
```

## Read APIs

| Method & path | Purpose |
|---|---|
| `GET /agents/online` | Agents online right now, with `online_since` and `online_secs`. |
| `GET /agents` | Latest presence state for every agent seen. |
| `GET /agents/{id}` | One agent's status + recent sessions. |
| `GET /agents/{id}/events` | Raw event audit log for one agent. |
| `GET /sessions?agentId=&date=YYYY-MM-DD&limit=` | Session history (login/logout times). |
| `GET /health` | Liveness check. |

## Durable persistence to Snowflake (optional)

SQLite stays the hot store; when Snowflake env vars are set, a background sink
also ships the **append-only audit logs** (`STATE_EVENTS`, `WORK_ITEM_EVENTS`)
and the `AGENTS` directory to Snowflake, so the data survives loss of the
server. SQLite doubles as the outbox: each row is marked `sf_synced_at` only
after a successful Snowflake `MERGE` (idempotent on event id). If Snowflake is
unreachable, rows stay unsynced locally and flush on a later cycle — no loss.

Enable by setting the env vars (see `.env.example`); leave them unset to run
SQLite-only. Sessions/work-items/reports remain SQLite-derived (reconstructable
from the event logs). Check it with:

```bash
python scripts/snowflake_init.py     # verify connection + create tables
curl http://127.0.0.1:8000/sink/status
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `data/presence.db` | SQLite file location. |
| `ZENDESK_WEBHOOK_SECRET` | _(unset)_ | Enables signature verification when set. |
| `ZENDESK_SUBDOMAIN` / `ZENDESK_EMAIL` / `ZENDESK_API_TOKEN` | _(unset)_ | Used by `scripts/sync_agents.py` to fetch the agent directory. |
| `SNOWFLAKE_ACCOUNT` / `SNOWFLAKE_USER` / `SNOWFLAKE_PASSWORD` | _(unset)_ | Snowflake creds; all required to enable the sink. |
| `SNOWFLAKE_WAREHOUSE` / `SNOWFLAKE_DATABASE` / `SNOWFLAKE_SCHEMA` | _(unset)_ | Target warehouse + where tables are created. |
| `SNOWFLAKE_ROLE` | _(unset)_ | Optional role. |
| `SNOWFLAKE_FLUSH_INTERVAL` / `SNOWFLAKE_BATCH` | `15` / `500` | Flush cadence (s) and rows per batch. |

## Notes

- Each event is processed in a single SQLite transaction guarded by a write
  lock, so the audit-log write and the derived state never diverge.
- Ordering: presence/work-item math uses the event's `updated_at` timestamp.
  Badly out-of-order deliveries are still stored in the audit logs, but the
  derived spans assume roughly ordered delivery (what Zendesk provides per agent).
- **Work-item payload field names** (`work_item_id`, `channel`, `reason`, …) are
  parsed defensively from real Zendesk docs but should be confirmed against your
  first live deliveries via the `work_item_events.raw` column.
- **Agent id mapping:** `sync_agents.py` stores the Zendesk user `id`. If your
  event `agent_id` is the omnichannel id instead, the name JOIN won't match —
  verify and adjust the mapping (reports still work, showing the id).
