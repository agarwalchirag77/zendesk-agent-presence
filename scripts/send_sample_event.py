"""Send sample agent state-change events to a running server.

Usage: python scripts/send_sample_event.py
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.request

URL = os.environ.get("URL", "http://localhost:8000/webhooks/zendesk/agent-state")
SECRET = os.environ.get("ZENDESK_WEBHOOK_SECRET", "")

# agent 10011: offline -> online (login), then online -> offline (logout).
EVENTS = [
    {
        "account_id": 2,
        "detail": {"account_id": "2", "agent_id": "10011", "version": "3"},
        "event": {
            "new_unified_state": {"id": "2", "name": "online", "reason": "AGENT_LOGIN"},
            "previous_unified_state": {"id": "1", "name": "offline", "reason": "UNKNOWN"},
            "updated_at": "2023-04-05T23:30:58.642630335Z",
        },
        "id": "01GX79ST1QX01C889XV42S2J5Z",
        "subject": "zen:agent:10011",
        "time": "2023-05-10T23:31:58.642630335Z",
        "type": "zen:event-type:agent.unified_state_changed",
        "zendesk_event_version": "2022-11-06",
    },
    {
        "account_id": 2,
        "detail": {"account_id": "2", "agent_id": "10011", "version": "4"},
        "event": {
            "new_unified_state": {"id": "1", "name": "offline", "reason": "AGENT_LOGOUT"},
            "previous_unified_state": {"id": "2", "name": "online", "reason": "AGENT_LOGIN"},
            "updated_at": "2023-04-06T01:15:00.000000000Z",
        },
        "id": "01GX79ST1QX01C889XV42S2J60",
        "subject": "zen:agent:10011",
        "time": "2023-04-06T01:15:01.000000000Z",
        "type": "zen:event-type:agent.unified_state_changed",
        "zendesk_event_version": "2022-11-06",
    },
]

# Work-item lifecycle for agent 10011: offered -> accepted -> solved.
# Drives the daily-workload report.
WORK_ITEM_EVENTS = [
    {
        "account_id": 2,
        "detail": {"account_id": "2", "agent_id": "10011"},
        "event": {
            "agent_id": "10011",
            "work_item_id": "wi-555",
            "channel": "messaging",
            "reason": "OFFERED",
            "updated_at": "2023-04-05T23:40:00.000000000Z",
        },
        "id": "01GX79WORKITEMADD0000000001",
        "subject": "zen:agent:10011",
        "time": "2023-04-05T23:40:00Z",
        "type": "zen:event-type:agent.work_item_added",
        "zendesk_event_version": "2022-11-06",
    },
    {
        "account_id": 2,
        "detail": {"account_id": "2", "agent_id": "10011"},
        "event": {
            "agent_id": "10011",
            "work_item_id": "wi-555",
            "channel": "messaging",
            "previous_reason": "OFFERED",
            "reason": "ACCEPTED",
            "updated_at": "2023-04-05T23:40:12.000000000Z",
        },
        "id": "01GX79WORKITEMUPD0000000001",
        "subject": "zen:agent:10011",
        "time": "2023-04-05T23:40:12Z",
        "type": "zen:event-type:agent.work_item_updated",
        "zendesk_event_version": "2022-11-06",
    },
    {
        "account_id": 2,
        "detail": {"account_id": "2", "agent_id": "10011"},
        "event": {
            "agent_id": "10011",
            "work_item_id": "wi-555",
            "channel": "messaging",
            "reason": "WORK_ITEM_SOLVED",
            "updated_at": "2023-04-06T00:10:00.000000000Z",
        },
        "id": "01GX79WORKITEMREM0000000001",
        "subject": "zen:agent:10011",
        "time": "2023-04-06T00:10:00Z",
        "type": "zen:event-type:agent.work_item_removed",
        "zendesk_event_version": "2022-11-06",
    },
]


def send(payload):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if SECRET:
        headers["X-Zendesk-Webhook-Signature"] = base64.b64encode(
            hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
        ).decode()
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    for e in EVENTS + WORK_ITEM_EVENTS:
        send(e)
