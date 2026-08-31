"""On-demand sync of the Zendesk agent directory into the `agents` table.

Reports LEFT JOIN this table so they can show agent names instead of numeric
ids. Run manually whenever the roster changes (or on a schedule):

    export ZENDESK_SUBDOMAIN=yourcompany        # the part before .zendesk.com
    export ZENDESK_EMAIL=you@company.com
    export ZENDESK_API_TOKEN=xxxxxxxx           # Admin > Apps and integrations > APIs > Zendesk API token
    python scripts/sync_agents.py

Auth uses Zendesk API-token Basic auth: "<email>/token:<api_token>".

NOTE (id mapping): this stores the Zendesk user `id` as agent_id. The id that
arrives in agent events may be the omnichannel/agent id instead. After syncing,
compare with a real event's agent_id; if they differ, map via the user's
external_id or the Agent Availability API. Reports fall back to the raw
agent_id when no name matches, so they stay correct in the meantime.
"""

import base64
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

# The macOS python.org build doesn't trust the system keychain, so verify TLS
# against certifi's CA bundle when available (falls back to the default context).
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL_CTX = ssl.create_default_context()

# Make the `app` package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn, write_lock  # noqa: E402

SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
EMAIL = os.environ.get("ZENDESK_EMAIL", "")
API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")


def _auth_header():
    raw = f"{EMAIL}/token:{API_TOKEN}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _fetch(url):
    req = urllib.request.Request(
        url, headers={"Authorization": _auth_header(), "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def fetch_agents():
    """Yield agent/admin user dicts, following cursor pagination."""
    base = f"https://{SUBDOMAIN}.zendesk.com/api/v2/users.json"
    url = f"{base}?role[]=agent&role[]=admin&page[size]=100"
    while url:
        data = _fetch(url)
        for user in data.get("users", []):
            yield user
        meta = data.get("meta") or {}
        links = data.get("links") or {}
        url = links.get("next") if meta.get("has_more") else None


def main():
    missing = [
        name
        for name, val in [
            ("ZENDESK_SUBDOMAIN", SUBDOMAIN),
            ("ZENDESK_EMAIL", EMAIL),
            ("ZENDESK_API_TOKEN", API_TOKEN),
        ]
        if not val
    ]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    count = 0
    with write_lock:
        for u in fetch_agents():
            conn.execute(
                """INSERT INTO agents (agent_id, name, email, role, active, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                       name = excluded.name, email = excluded.email,
                       role = excluded.role, active = excluded.active,
                       synced_at = excluded.synced_at""",
                (
                    str(u.get("id")),
                    u.get("name"),
                    u.get("email"),
                    u.get("role"),
                    1 if u.get("active") else 0,
                    now,
                ),
            )
            count += 1
        conn.commit()
    print(f"Synced {count} agents into the agents table.")


if __name__ == "__main__":
    main()
