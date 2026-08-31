"""FastAPI webhook server for tracking Zendesk support-agent presence."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import snowflake_sink
from .db import get_conn
from .events import (
    _parse_iso,
    handle_event,
    handle_work_item_event,
    parse_event,
)
from .timeutil import ist_day_bounds, to_ist, yesterday_ist

# Optional shared secret. If ZENDESK_WEBHOOK_SECRET is set, every webhook
# request must carry a matching HMAC-SHA256 signature of the raw body in the
# X-Zendesk-Webhook-Signature header (base64). If unset, verification is
# skipped (handy for local testing).
WEBHOOK_SECRET = os.environ.get("ZENDESK_WEBHOOK_SECRET", "")

app = FastAPI(title="Zendesk Agent Presence Tracker", version="1.0.0")


def _verify_signature(raw_body: bytes, provided: Optional[str]) -> bool:
    if not WEBHOOK_SECRET:
        return True  # verification disabled
    if not provided:
        return False
    import base64

    expected = base64.b64encode(
        hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(provided, expected)


def _rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


@app.on_event("startup")
def _start_snowflake_sink():
    # No-op unless SNOWFLAKE_* env vars are set (feature is opt-in).
    snowflake_sink.start()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sink/status")
def sink_status():
    """Snowflake sink observability: enabled?, unsynced backlog, last flush/error."""
    return snowflake_sink.status()


# --- Webhook ingestion -----------------------------------------------------
@app.post("/webhooks/zendesk/agent-state")
async def ingest(request: Request):
    raw_body = await request.body()
    sig = request.headers.get("x-zendesk-webhook-signature")
    if not _verify_signature(raw_body, sig):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        body = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # Zendesk may deliver a single event object or an array of events.
    payloads = body if isinstance(body, list) else [body]
    results = []

    for payload in payloads:
        evt = parse_event(payload)
        if not evt:
            results.append(
                {"accepted": False, "reason": "unrecognized or non-agent-state event"}
            )
            continue
        try:
            handler = (
                handle_work_item_event
                if evt.get("_route") == "work_item"
                else handle_event
            )
            r = handler(evt, json.dumps(payload))
            results.append({"accepted": True, **r})
        except Exception as exc:  # noqa: BLE001 - log and keep ingesting
            print(f"Failed to handle event {evt.get('event_id')}: {exc}")
            results.append(
                {"accepted": False, "event_id": evt.get("event_id"), "reason": "processing error"}
            )

    # Always 200 once accepted so Zendesk doesn't retry valid deliveries.
    return JSONResponse({"received": len(payloads), "results": results})


# --- Read APIs -------------------------------------------------------------


@app.get("/agents/online")
def agents_online():
    """Who is online right now, and since when."""
    conn = get_conn()
    rows = _rows(
        conn.execute(
            """SELECT agent_id, account_id, login_at AS online_since,
                      CAST((julianday('now') - julianday(login_at)) * 86400 AS INTEGER) AS online_secs
               FROM agent_sessions
               WHERE logout_at IS NULL
               ORDER BY login_at ASC"""
        )
    )
    return {"count": len(rows), "agents": rows}


@app.get("/agents")
def list_agents():
    """Current presence state for every agent we've ever seen."""
    conn = get_conn()
    rows = _rows(conn.execute("SELECT * FROM agent_status ORDER BY agent_id"))
    return {"count": len(rows), "agents": rows}


@app.get("/agents/{agent_id}")
def agent_detail(agent_id: str):
    """Status + recent sessions for one agent."""
    conn = get_conn()
    status = conn.execute(
        "SELECT * FROM agent_status WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if not status:
        raise HTTPException(status_code=404, detail="unknown agent")
    sessions = _rows(
        conn.execute(
            "SELECT * FROM agent_sessions WHERE agent_id = ? ORDER BY id DESC LIMIT 50",
            (agent_id,),
        )
    )
    return {"status": dict(status), "sessions": sessions}


@app.get("/agents/{agent_id}/events")
def agent_events(agent_id: str):
    """Raw event audit log for one agent (debugging / traceability)."""
    conn = get_conn()
    rows = _rows(
        conn.execute(
            """SELECT event_id, previous_state_name, new_state_name, new_reason,
                      state_updated_at, received_at
               FROM state_events WHERE agent_id = ? ORDER BY state_updated_at DESC LIMIT 100""",
            (agent_id,),
        )
    )
    return {"count": len(rows), "events": rows}


@app.get("/sessions")
def sessions(
    agentId: Optional[str] = None,
    date: Optional[str] = Query(None, description="Filter login_at by YYYY-MM-DD"),
    limit: int = 200,
):
    """Session history across the team. logout_at == "online till what time"."""
    conn = get_conn()
    clauses, params = [], []
    if agentId:
        clauses.append("agent_id = ?")
        params.append(agentId)
    if date:
        clauses.append("substr(login_at, 1, 10) = ?")
        params.append(date)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    lim = min(max(limit, 1), 1000)
    rows = _rows(
        conn.execute(
            f"SELECT * FROM agent_sessions {where} ORDER BY login_at DESC LIMIT ?",
            (*params, lim),
        )
    )
    return {"count": len(rows), "sessions": rows}


# --- Daily reports (IST, with agent names) ---------------------------------


def _hms(secs):
    """Format a duration in seconds as HH:MM:SS (None-safe)."""
    if secs is None:
        return None
    secs = int(secs)
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


@app.get("/reports/daily-status")
def daily_status(
    date: Optional[str] = Query(None, description="IST day YYYY-MM-DD; default yesterday IST")
):
    """Per-agent presence for one IST day: when they came online, last logout,
    and total online time within the day (online = available on any channel)."""
    day = date or yesterday_ist()
    start_utc, end_utc = ist_day_bounds(day)
    now = datetime.now(timezone.utc)
    conn = get_conn()
    rows = _rows(
        conn.execute(
            """SELECT s.agent_id, s.login_at, s.logout_at, a.name AS agent_name
               FROM agent_sessions s
               LEFT JOIN agents a ON a.agent_id = s.agent_id"""
        )
    )

    agg = {}
    for r in rows:
        login = _parse_iso(r["login_at"])
        if login is None:
            continue
        logout = _parse_iso(r["logout_at"]) if r["logout_at"] else now
        ov_start = max(login, start_utc)
        ov_end = min(logout, end_utc)
        if ov_end <= ov_start:
            continue  # session doesn't overlap this IST day

        a = agg.setdefault(
            r["agent_id"],
            {
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"] or r["agent_id"],
                "online_secs": 0,
                "session_count": 0,
                "_first": None,
                "_last": None,
                "_open": False,
            },
        )
        a["online_secs"] += int((ov_end - ov_start).total_seconds())
        a["session_count"] += 1
        if a["_first"] is None or login < a["_first"]:
            a["_first"] = login
        if r["logout_at"]:
            if a["_last"] is None or logout > a["_last"]:
                a["_last"] = logout
        else:
            a["_open"] = True  # still online

    agents = []
    for a in agg.values():
        agents.append(
            {
                "agent_id": a["agent_id"],
                "agent_name": a["agent_name"],
                "first_login_ist": to_ist(a["_first"].isoformat()) if a["_first"] else None,
                "last_logout_ist": None
                if a["_open"]
                else (to_ist(a["_last"].isoformat()) if a["_last"] else None),
                "still_online": a["_open"],
                "online_secs": a["online_secs"],
                "online_hms": _hms(a["online_secs"]),
                "session_count": a["session_count"],
            }
        )
    agents.sort(key=lambda x: (x["agent_name"] or "").lower())
    return {"date": day, "tz": "IST", "count": len(agents), "agents": agents}


@app.get("/reports/daily-workload")
def daily_workload(
    date: Optional[str] = Query(None, description="IST day YYYY-MM-DD; default yesterday IST")
):
    """Per-agent work handled on one IST day: items added/completed, per-channel
    breakdown, total/avg handle time, and accepted vs offered-only counts."""
    day = date or yesterday_ist()
    start_utc, end_utc = ist_day_bounds(day)
    conn = get_conn()
    rows = _rows(
        conn.execute(
            """SELECT w.*, a.name AS agent_name
               FROM work_items w
               LEFT JOIN agents a ON a.agent_id = w.agent_id"""
        )
    )

    agg = {}
    for r in rows:
        added = _parse_iso(r["added_at"])
        removed = _parse_iso(r["removed_at"]) if r["removed_at"] else None
        added_in_day = added is not None and start_utc <= added < end_utc
        removed_in_day = removed is not None and start_utc <= removed < end_utc
        if not (added_in_day or removed_in_day):
            continue

        a = agg.setdefault(
            r["agent_id"],
            {
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"] or r["agent_id"],
                "items_added": 0,
                "items_completed": 0,
                "by_channel": {},
                "accepted_count": 0,
                "offered_only_count": 0,
                "total_handle_secs": 0,
                "_handled_n": 0,
            },
        )
        if added_in_day:
            a["items_added"] += 1
            ch = r["channel"] or "unknown"
            a["by_channel"][ch] = a["by_channel"].get(ch, 0) + 1
            if r["accepted_at"]:
                a["accepted_count"] += 1
            else:
                a["offered_only_count"] += 1
        if removed_in_day:
            a["items_completed"] += 1
            if r["handle_secs"] is not None:
                a["total_handle_secs"] += r["handle_secs"]
                a["_handled_n"] += 1

    agents = []
    for a in agg.values():
        n = a.pop("_handled_n")
        avg = round(a["total_handle_secs"] / n) if n else 0
        a["avg_handle_secs"] = avg
        a["avg_handle_hms"] = _hms(avg)
        a["total_handle_hms"] = _hms(a["total_handle_secs"])
        agents.append(a)
    agents.sort(key=lambda x: (x["agent_name"] or "").lower())
    return {"date": day, "tz": "IST", "count": len(agents), "agents": agents}
