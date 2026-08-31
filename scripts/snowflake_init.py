"""Verify the Snowflake connection and create the audit tables.

Optional one-time helper — the app's background sink also creates the tables on
startup, but this lets you validate credentials/permissions up front:

    export SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=... \
           SNOWFLAKE_WAREHOUSE=... SNOWFLAKE_DATABASE=... SNOWFLAKE_SCHEMA=... \
           SNOWFLAKE_ROLE=...            # role optional
    python scripts/snowflake_init.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import snowflake_sink  # noqa: E402


def main():
    if not snowflake_sink.is_enabled():
        sys.exit(
            "Missing required env vars. Need: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, "
            "SNOWFLAKE_PASSWORD, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA."
        )
    conn = snowflake_sink._connect()
    print("Connected to Snowflake.")
    snowflake_sink._ensure_tables(conn)
    print(f"Ensured tables in {snowflake_sink.DATABASE}.{snowflake_sink.SCHEMA}: "
          "STATE_EVENTS, WORK_ITEM_EVENTS, AGENTS")
    cur = conn.cursor()
    for t in ("STATE_EVENTS", "WORK_ITEM_EVENTS", "AGENTS"):
        n = cur.execute(f"SELECT COUNT(*) FROM {snowflake_sink._fq(t)}").fetchone()[0]
        print(f"  {t}: {n} rows")
    cur.close()
    conn.close()
    print("OK.")


if __name__ == "__main__":
    main()
