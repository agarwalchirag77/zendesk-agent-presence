"""Durable Snowflake sink for the append-only audit logs.

SQLite stays the hot store and the outbox: rows are inserted locally as usual
(sf_synced_at IS NULL), and this background flusher ships unsynced rows to
Snowflake, marking them synced only after a successful MERGE. If Snowflake is
unreachable the rows simply stay unsynced and flush on a later cycle — the
buffer+retry guarantee. Idempotent via MERGE on the event id.

Opt-in: if SNOWFLAKE_ACCOUNT (and the other required vars) are not set, the
sink is disabled and the app behaves exactly as a SQLite-only deployment.
"""

import os
import threading
import time
from datetime import datetime, timezone

from . import db

# --- config from env -------------------------------------------------------
ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "")
USER = os.environ.get("SNOWFLAKE_USER", "")
PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "")
DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "")
SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA", "")
ROLE = os.environ.get("SNOWFLAKE_ROLE", "")
FLUSH_INTERVAL = int(os.environ.get("SNOWFLAKE_FLUSH_INTERVAL", "15"))
BATCH = int(os.environ.get("SNOWFLAKE_BATCH", "500"))

_REQUIRED = [ACCOUNT, USER, PASSWORD, WAREHOUSE, DATABASE, SCHEMA]


def is_enabled() -> bool:
    return all(_REQUIRED)


# Column order mirrors the SQLite audit tables (sf_synced_at is local-only).
STATE_COLS = [
    "event_id", "account_id", "agent_id",
    "previous_state_id", "previous_state_name", "previous_reason",
    "new_state_id", "new_state_name", "new_reason",
    "state_updated_at", "event_time", "received_at", "raw",
]
WI_COLS = [
    "event_id", "account_id", "agent_id", "work_item_id", "channel",
    "kind", "reason", "previous_reason", "item_updated_at",
    "event_time", "received_at", "raw",
]
AGENT_COLS = ["agent_id", "name", "email", "role", "active", "synced_at"]

# (sqlite_table, snowflake_table, columns)
_EVENT_SPECS = [
    ("state_events", "STATE_EVENTS", STATE_COLS),
    ("work_item_events", "WORK_ITEM_EVENTS", WI_COLS),
]

_status = {
    "enabled": is_enabled(),
    "last_flush_at": None,
    "last_error": None,
    "last_synced": 0,
    "running": False,
}
_status_lock = threading.Lock()
_sf_conn = None
_started = False
_start_lock = threading.Lock()
_last_agents_marker = None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fq(table: str) -> str:
    return f"{DATABASE}.{SCHEMA}.{table}"


def _connect():
    import snowflake.connector  # imported lazily so the app runs without it

    snowflake.connector.paramstyle = "qmark"
    kwargs = dict(
        account=ACCOUNT, user=USER, password=PASSWORD,
        warehouse=WAREHOUSE, database=DATABASE, schema=SCHEMA,
        login_timeout=20, network_timeout=30,
    )
    if ROLE:
        kwargs["role"] = ROLE
    return snowflake.connector.connect(**kwargs)


def _ensure_tables(conn):
    cur = conn.cursor()
    ev_cols_ddl = ", ".join(f"{c.upper()} VARCHAR" for c in STATE_COLS)
    cur.execute(f"CREATE TABLE IF NOT EXISTS {_fq('STATE_EVENTS')} ({ev_cols_ddl})")
    wi_cols_ddl = ", ".join(f"{c.upper()} VARCHAR" for c in WI_COLS)
    cur.execute(f"CREATE TABLE IF NOT EXISTS {_fq('WORK_ITEM_EVENTS')} ({wi_cols_ddl})")
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_fq('AGENTS')} "
        "(AGENT_ID VARCHAR, NAME VARCHAR, EMAIL VARCHAR, ROLE VARCHAR, "
        "ACTIVE NUMBER, SYNCED_AT VARCHAR)"
    )
    cur.close()


def _flush_events(conn, sqlite_table, sf_table, cols) -> int:
    """MERGE one batch of unsynced rows into Snowflake; mark them synced."""
    colnames = ", ".join(cols)
    # 1) read a batch under the write lock (consistent snapshot, short hold)
    with db.write_lock:
        rows = db.get_conn().execute(
            f"SELECT {colnames} FROM {sqlite_table} "
            f"WHERE sf_synced_at IS NULL ORDER BY rowid LIMIT {BATCH}"
        ).fetchall()
    if not rows:
        return 0
    tuples = [tuple(r[c] for c in cols) for r in rows]

    # 2) stage + MERGE in Snowflake (network I/O, no sqlite lock held)
    stage = f"STAGE_{sf_table}"
    cur = conn.cursor()
    cur.execute(f"CREATE TEMPORARY TABLE IF NOT EXISTS {stage} LIKE {_fq(sf_table)}")
    cur.execute(f"TRUNCATE TABLE {stage}")
    placeholders = "(" + ", ".join(["?"] * len(cols)) + ")"
    cur.executemany(
        f"INSERT INTO {stage} ({colnames}) VALUES {placeholders}", tuples
    )
    set_clause = " AND ".join([f"t.EVENT_ID = s.EVENT_ID"])
    insert_cols = ", ".join(c.upper() for c in cols)
    insert_vals = ", ".join(f"s.{c.upper()}" for c in cols)
    cur.execute(
        f"MERGE INTO {_fq(sf_table)} t USING {stage} s ON {set_clause} "
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
    )
    conn.commit()
    cur.close()

    # 3) mark synced locally, under the write lock
    stamp = _now()
    ids = [r["event_id"] for r in rows]
    with db.write_lock:
        db.get_conn().executemany(
            f"UPDATE {sqlite_table} SET sf_synced_at = ? WHERE event_id = ?",
            [(stamp, i) for i in ids],
        )
        db.get_conn().commit()
    return len(rows)


def _flush_agents(conn) -> None:
    """MERGE the whole agents directory when it has changed (small table)."""
    global _last_agents_marker
    with db.write_lock:
        marker = db.get_conn().execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(synced_at),'') AS m FROM agents"
        ).fetchone()
        marker = (marker["n"], marker["m"])
        if marker == _last_agents_marker:
            return
        rows = db.get_conn().execute(
            "SELECT agent_id, name, email, role, active, synced_at FROM agents"
        ).fetchall()
    if not rows:
        _last_agents_marker = marker
        return
    tuples = [tuple(r[c] for c in AGENT_COLS) for r in rows]
    cur = conn.cursor()
    cur.execute(f"CREATE TEMPORARY TABLE IF NOT EXISTS STAGE_AGENTS LIKE {_fq('AGENTS')}")
    cur.execute("TRUNCATE TABLE STAGE_AGENTS")
    cur.executemany(
        "INSERT INTO STAGE_AGENTS (AGENT_ID, NAME, EMAIL, ROLE, ACTIVE, SYNCED_AT) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        tuples,
    )
    cur.execute(
        f"MERGE INTO {_fq('AGENTS')} t USING STAGE_AGENTS s ON t.AGENT_ID = s.AGENT_ID "
        "WHEN MATCHED THEN UPDATE SET NAME=s.NAME, EMAIL=s.EMAIL, ROLE=s.ROLE, "
        "ACTIVE=s.ACTIVE, SYNCED_AT=s.SYNCED_AT "
        "WHEN NOT MATCHED THEN INSERT (AGENT_ID, NAME, EMAIL, ROLE, ACTIVE, SYNCED_AT) "
        "VALUES (s.AGENT_ID, s.NAME, s.EMAIL, s.ROLE, s.ACTIVE, s.SYNCED_AT)"
    )
    conn.commit()
    cur.close()
    _last_agents_marker = marker


def flush_once() -> int:
    """Ensure a connection + tables, flush both event tables and agents."""
    global _sf_conn
    if _sf_conn is None:
        _sf_conn = _connect()
        _ensure_tables(_sf_conn)
    total = 0
    for sqlite_table, sf_table, cols in _EVENT_SPECS:
        # loop so a large backlog drains across multiple batches
        while True:
            n = _flush_events(_sf_conn, sqlite_table, sf_table, cols)
            total += n
            if n < BATCH:
                break
    _flush_agents(_sf_conn)
    return total


def _run_loop():
    global _sf_conn
    with _status_lock:
        _status["running"] = True
    while True:
        try:
            n = flush_once()
            with _status_lock:
                _status["last_flush_at"] = _now()
                _status["last_synced"] = n
                _status["last_error"] = None
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, retry next cycle
            with _status_lock:
                _status["last_error"] = f"{type(exc).__name__}: {exc}"
            try:
                if _sf_conn is not None:
                    _sf_conn.close()
            except Exception:
                pass
            _sf_conn = None  # force reconnect next cycle
        time.sleep(FLUSH_INTERVAL)


def start():
    """Start the background flusher once, if the sink is enabled."""
    global _started
    if not is_enabled():
        return
    with _start_lock:
        if _started:
            return
        try:
            import snowflake.connector  # noqa: F401 - fail fast if missing
        except Exception as exc:  # noqa: BLE001
            with _status_lock:
                _status["enabled"] = False
                _status["last_error"] = f"snowflake-connector import failed: {exc}"
            return
        t = threading.Thread(target=_run_loop, name="snowflake-sink", daemon=True)
        t.start()
        _started = True


def status() -> dict:
    """Snapshot for the /sink/status endpoint."""
    out = dict(_status)
    out["enabled"] = is_enabled()
    if is_enabled():
        with db.write_lock:
            conn = db.get_conn()
            out["unsynced"] = {
                "state_events": conn.execute(
                    "SELECT COUNT(*) FROM state_events WHERE sf_synced_at IS NULL"
                ).fetchone()[0],
                "work_item_events": conn.execute(
                    "SELECT COUNT(*) FROM work_item_events WHERE sf_synced_at IS NULL"
                ).fetchone()[0],
            }
        out["config"] = {
            "database": DATABASE, "schema": SCHEMA,
            "warehouse": WAREHOUSE, "flush_interval": FLUSH_INTERVAL,
        }
    return out
