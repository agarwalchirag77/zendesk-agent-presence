"""SQLite storage for the Zendesk agent-presence tracker.

A single module-level connection is shared across requests, guarded by a lock
for writes. WAL mode keeps reads concurrent with the occasional write.
"""

import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "presence.db"),
)

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode = WAL")
_conn.execute("PRAGMA foreign_keys = ON")

# Serialize writes; sqlite is fine for concurrent reads under WAL.
write_lock = threading.Lock()

SCHEMA = """
-- Append-only audit log of every state-change event we receive.
-- event_id is the Zendesk event id, used to make ingestion idempotent.
CREATE TABLE IF NOT EXISTS state_events (
    event_id            TEXT PRIMARY KEY,
    account_id          TEXT,
    agent_id            TEXT NOT NULL,
    previous_state_id   TEXT,
    previous_state_name TEXT,
    previous_reason     TEXT,
    new_state_id        TEXT,
    new_state_name      TEXT NOT NULL,
    new_reason          TEXT,
    state_updated_at    TEXT,   -- event.updated_at (when the state actually changed)
    event_time          TEXT,   -- top-level "time" of the event
    received_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    raw                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_events_agent ON state_events(agent_id, state_updated_at);

-- One row per agent: their latest known presence state.
CREATE TABLE IF NOT EXISTS agent_status (
    agent_id        TEXT PRIMARY KEY,
    account_id      TEXT,
    current_state   TEXT NOT NULL,          -- e.g. online / offline / away
    current_reason  TEXT,
    since           TEXT,                   -- when they entered current_state
    last_event_id   TEXT,
    last_event_at   TEXT
);

-- Online sessions: one row per continuous period the agent was NOT offline.
-- logout_at IS NULL  => the agent is currently online ("online till now").
CREATE TABLE IF NOT EXISTS agent_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    account_id      TEXT,
    login_at        TEXT NOT NULL,
    logout_at       TEXT,
    login_event_id  TEXT,
    logout_event_id TEXT,
    duration_secs   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON agent_sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON agent_sessions(agent_id, logout_at);

-- Append-only audit log of every work-item event (added/updated/removed).
-- Idempotent on the Zendesk event id, like state_events.
CREATE TABLE IF NOT EXISTS work_item_events (
    event_id        TEXT PRIMARY KEY,
    account_id      TEXT,
    agent_id        TEXT NOT NULL,
    work_item_id    TEXT,
    channel         TEXT,
    kind            TEXT NOT NULL,          -- added / updated / removed
    reason          TEXT,
    previous_reason TEXT,
    item_updated_at TEXT,                   -- event.updated_at
    event_time      TEXT,                   -- top-level "time"
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    raw             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wie_agent ON work_item_events(agent_id, item_updated_at);

-- One row per (agent, work-item) handling span. removed_at IS NULL => still
-- being handled. handle_secs is filled when the item is removed.
CREATE TABLE IF NOT EXISTS work_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    work_item_id    TEXT,
    channel         TEXT,
    added_at        TEXT NOT NULL,
    accepted_at     TEXT,                   -- when reason became ACCEPTED
    removed_at      TEXT,
    last_reason     TEXT,
    removal_reason  TEXT,
    handle_secs     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wi_open ON work_items(agent_id, work_item_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_wi_added ON work_items(agent_id, added_at);

-- Agent directory, populated on demand by scripts/sync_agents.py so reports
-- can show names instead of numeric ids.
CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT PRIMARY KEY,
    name       TEXT,
    email      TEXT,
    role       TEXT,
    active     INTEGER,
    synced_at  TEXT
);
"""

_conn.executescript(SCHEMA)
_conn.commit()


def _ensure_column(table: str, column: str, decl: str) -> None:
    """Add a column if it isn't there yet (idempotent lightweight migration)."""
    cols = [r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        _conn.commit()


# Outbox marker for the Snowflake sink: NULL = not yet shipped to Snowflake.
# (Named sf_synced_at to avoid clashing with agents.synced_at, which is the
# Zendesk-directory sync time.)
_ensure_column("state_events", "sf_synced_at", "TEXT")
_ensure_column("work_item_events", "sf_synced_at", "TEXT")
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_se_unsynced ON state_events(sf_synced_at)"
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_wie_unsynced ON work_item_events(sf_synced_at)"
)
_conn.commit()


def get_conn() -> sqlite3.Connection:
    return _conn
