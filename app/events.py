"""Parsing and session logic for Zendesk agent events.

Handles two families:
  * agent.unified_state_changed  -> presence sessions (agent_sessions)
  * agent.work_item_{added,updated,removed} -> work-item handling spans
"""

import json
import re
from datetime import datetime, timezone

from .db import get_conn, write_lock

# Zendesk unified states other than "offline" all mean the agent is logged in
# and reachable in some way (online / away / transfers_only ...). We treat any
# non-offline state as "online" for session tracking.
OFFLINE = "offline"

# Map the work-item event-type fragment to a short kind.
WORK_ITEM_KINDS = {
    "work_item_added": "added",
    "work_item_updated": "updated",
    "work_item_removed": "removed",
}

_SUBJECT_RE = re.compile(r"agent:(\d+)")


def _is_offline(state_name) -> bool:
    return str(state_name or "").lower() == OFFLINE


def _s(v):
    return None if v is None else str(v)


def _extract_agent_id(payload, detail):
    """agent_id from detail.agent_id, else the subject "zen:agent:<id>"."""
    agent_id = detail.get("agent_id")
    if not agent_id:
        event = payload.get("event") or {}
        agent_id = event.get("agent_id")
    if not agent_id and isinstance(payload.get("subject"), str):
        m = _SUBJECT_RE.search(payload["subject"])
        if m:
            agent_id = m.group(1)
    return str(agent_id) if agent_id else None


def _extract_account_id(payload, detail):
    account_id = detail.get("account_id")
    if account_id is None and payload.get("account_id") is not None:
        account_id = str(payload["account_id"])
    return account_id


def parse_event(payload):
    """Parse a raw event payload into a normalized dict, routed by type.

    Returns a dict with a "_route" key ("presence" or "work_item"), or None
    if the payload is not an agent event we handle.
    """
    if not isinstance(payload, dict):
        return None

    event_type = payload.get("type", "") or ""

    # Work-item events.
    for fragment, kind in WORK_ITEM_KINDS.items():
        if fragment in event_type:
            return _parse_work_item(payload, kind)

    # Presence: explicit unified-state type, or (back-compat) a missing type.
    if not event_type or "agent.unified_state_changed" in event_type:
        return _parse_unified_state(payload)

    return None  # some other agent event we don't track


def _parse_unified_state(payload):
    event = payload.get("event") or {}
    new_state = event.get("new_unified_state") or {}
    prev_state = event.get("previous_unified_state") or {}
    detail = payload.get("detail") or {}

    agent_id = _extract_agent_id(payload, detail)
    if not agent_id:
        return None

    return {
        "_route": "presence",
        "event_id": payload.get("id"),
        "account_id": _extract_account_id(payload, detail),
        "agent_id": agent_id,
        "previous_state_id": _s(prev_state.get("id")),
        "previous_state_name": prev_state.get("name"),
        "previous_reason": prev_state.get("reason"),
        "new_state_id": _s(new_state.get("id")),
        "new_state_name": new_state.get("name"),
        "new_reason": new_state.get("reason"),
        "state_updated_at": event.get("updated_at"),
        "event_time": payload.get("time"),
    }


def _parse_work_item(payload, kind):
    """Parse an agent.work_item_* event.

    Field names are read defensively from payload.event with sensible
    fallbacks; the full raw payload is always stored so parsing can be
    corrected once real Zendesk deliveries are observed.
    """
    event = payload.get("event") or {}
    detail = payload.get("detail") or {}

    agent_id = _extract_agent_id(payload, detail)
    if not agent_id:
        return None

    return {
        "_route": "work_item",
        "kind": kind,
        "event_id": payload.get("id"),
        "account_id": _extract_account_id(payload, detail),
        "agent_id": agent_id,
        "work_item_id": _s(event.get("work_item_id") or event.get("id")),
        "channel": event.get("channel"),
        "reason": event.get("reason"),
        "previous_reason": event.get("previous_reason"),
        "item_updated_at": event.get("updated_at"),
        "event_time": payload.get("time"),
    }


def _seconds_between(start_iso, end_iso):
    try:
        start = _parse_iso(start_iso)
        end = _parse_iso(end_iso)
    except (ValueError, TypeError):
        return None
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _parse_iso(value):
    """Parse an ISO-8601 timestamp, tolerating Zendesk's nanosecond precision."""
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    # Python's fromisoformat handles at most microseconds; trim extra digits.
    m = re.match(r"(.*\.\d{6})\d*(.*)", s)
    if m:
        s = m.group(1) + m.group(2)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def handle_event(evt: dict, raw_json: str) -> dict:
    """Process one parsed event in a single transaction.

    - append to the audit log (idempotent on event_id)
    - update the agent's current status
    - open/close online sessions on offline<->non-offline transitions

    Returns {"duplicate": bool, "agent_id", "new_state", "session_action"}.
    """
    conn = get_conn()
    with write_lock:
        # Idempotency: skip if we've already stored this event id.
        if evt["event_id"]:
            exists = conn.execute(
                "SELECT 1 FROM state_events WHERE event_id = ?", (evt["event_id"],)
            ).fetchone()
            if exists:
                return {
                    "duplicate": True,
                    "agent_id": evt["agent_id"],
                    "new_state": evt["new_state_name"],
                    "session_action": "none",
                }

        try:
            conn.execute(
                """INSERT INTO state_events (
                       event_id, account_id, agent_id,
                       previous_state_id, previous_state_name, previous_reason,
                       new_state_id, new_state_name, new_reason,
                       state_updated_at, event_time, raw
                   ) VALUES (
                       :event_id, :account_id, :agent_id,
                       :previous_state_id, :previous_state_name, :previous_reason,
                       :new_state_id, :new_state_name, :new_reason,
                       :state_updated_at, :event_time, :raw
                   )""",
                {**evt, "raw": raw_json},
            )

            stamp = evt["state_updated_at"] or evt["event_time"]

            conn.execute(
                """INSERT INTO agent_status
                       (agent_id, account_id, current_state, current_reason, since, last_event_id, last_event_at)
                   VALUES (:agent_id, :account_id, :new_state_name, :new_reason, :since, :event_id, :since)
                   ON CONFLICT(agent_id) DO UPDATE SET
                       account_id     = excluded.account_id,
                       current_state  = excluded.current_state,
                       current_reason = excluded.current_reason,
                       since          = excluded.since,
                       last_event_id  = excluded.last_event_id,
                       last_event_at  = excluded.last_event_at""",
                {**evt, "since": stamp},
            )

            session_action = "none"
            now_offline = _is_offline(evt["new_state_name"])
            open_session = conn.execute(
                "SELECT * FROM agent_sessions WHERE agent_id = ? AND logout_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (evt["agent_id"],),
            ).fetchone()

            if now_offline:
                # Logout: close any open session.
                if open_session:
                    conn.execute(
                        "UPDATE agent_sessions SET logout_at = ?, logout_event_id = ?, "
                        "duration_secs = ? WHERE id = ?",
                        (
                            stamp,
                            evt["event_id"],
                            _seconds_between(open_session["login_at"], stamp),
                            open_session["id"],
                        ),
                    )
                    session_action = "closed"
            else:
                # Non-offline state. Open a session only if none is open, so
                # online -> away -> online stays a single continuous session.
                if not open_session:
                    conn.execute(
                        "INSERT INTO agent_sessions (agent_id, account_id, login_at, login_event_id) "
                        "VALUES (?, ?, ?, ?)",
                        (evt["agent_id"], evt["account_id"], stamp, evt["event_id"]),
                    )
                    session_action = "opened"

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "duplicate": False,
        "agent_id": evt["agent_id"],
        "new_state": evt["new_state_name"],
        "session_action": session_action,
    }


def handle_work_item_event(evt: dict, raw_json: str) -> dict:
    """Process one work-item event in a single transaction.

    - append to the work_item_events audit log (idempotent on event_id)
    - maintain a per-(agent, work-item) handling span in work_items:
        added   -> open a span (added_at)
        updated -> update last_reason; stamp accepted_at on ACCEPTED
        removed -> close the span (removed_at, handle_secs)

    Returns {"duplicate", "agent_id", "kind", "work_item_action"}.
    """
    conn = get_conn()
    kind = evt["kind"]
    with write_lock:
        if evt["event_id"]:
            exists = conn.execute(
                "SELECT 1 FROM work_item_events WHERE event_id = ?", (evt["event_id"],)
            ).fetchone()
            if exists:
                return {
                    "duplicate": True,
                    "agent_id": evt["agent_id"],
                    "kind": kind,
                    "work_item_action": "none",
                }

        try:
            conn.execute(
                """INSERT INTO work_item_events (
                       event_id, account_id, agent_id, work_item_id, channel,
                       kind, reason, previous_reason, item_updated_at, event_time, raw
                   ) VALUES (
                       :event_id, :account_id, :agent_id, :work_item_id, :channel,
                       :kind, :reason, :previous_reason, :item_updated_at, :event_time, :raw
                   )""",
                {**evt, "raw": raw_json},
            )

            stamp = evt["item_updated_at"] or evt["event_time"]
            wid = evt["work_item_id"]
            reason = evt.get("reason")
            is_accepted = str(reason or "").upper() == "ACCEPTED"
            action = "logged"

            open_row = None
            if wid is not None:
                open_row = conn.execute(
                    "SELECT * FROM work_items WHERE agent_id = ? AND work_item_id = ? "
                    "AND removed_at IS NULL ORDER BY id DESC LIMIT 1",
                    (evt["agent_id"], wid),
                ).fetchone()

            if kind == "removed":
                if open_row:
                    conn.execute(
                        "UPDATE work_items SET removed_at = ?, removal_reason = ?, "
                        "handle_secs = ?, last_reason = ? WHERE id = ?",
                        (
                            stamp,
                            reason,
                            _seconds_between(open_row["added_at"], stamp),
                            reason,
                            open_row["id"],
                        ),
                    )
                    action = "closed"
                else:
                    action = "closed_no_open"
            else:
                # added / updated
                if open_row:
                    conn.execute(
                        "UPDATE work_items SET last_reason = ?, channel = COALESCE(?, channel), "
                        "accepted_at = COALESCE(accepted_at, ?) WHERE id = ?",
                        (
                            reason,
                            evt["channel"],
                            stamp if is_accepted else None,
                            open_row["id"],
                        ),
                    )
                    action = "updated"
                else:
                    # No open span yet (added, or updated arriving first).
                    conn.execute(
                        "INSERT INTO work_items (agent_id, work_item_id, channel, "
                        "added_at, accepted_at, last_reason) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            evt["agent_id"],
                            wid,
                            evt["channel"],
                            stamp,
                            stamp if is_accepted else None,
                            reason,
                        ),
                    )
                    action = "opened"

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "duplicate": False,
        "agent_id": evt["agent_id"],
        "kind": kind,
        "work_item_action": action,
    }
