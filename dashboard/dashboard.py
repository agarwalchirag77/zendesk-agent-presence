"""Self-hosted Streamlit dashboard (runs on the EC2, reads Snowflake).

Serves presence + shift-compliance reports and an ad-hoc SQL console over a
browser port so viewers don't need SSH/pem access. Connects to Snowflake with
the same SNOWFLAKE_* env vars the app's sink uses, and reads the V_* views
created by deploy/snowflake_dashboard.sql (Part 1).

Run locally:   streamlit run dashboard/dashboard.py
On the EC2 it runs as the `zendesk-dashboard` systemd service (see
deploy/dashboard_setup.sh), bound to 0.0.0.0:8501.

Access control:
  - Set DASHBOARD_PASSWORD to gate the page (recommended).
  - Restrict inbound :8501 in the EC2 security group to your internal/VPN CIDR.
"""

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

st.set_page_config(page_title="Agent Presence Dashboard", layout="wide")

# Shift windows (IST): start h,m  end h,m  end-day-offset (Night ends next day).
SHIFTS = {
    "M": (5, 0, 14, 0, 0), "A": (14, 0, 23, 0, 0), "N": (23, 0, 5, 0, 1),
    "D": (8, 0, 17, 0, 0), "E": (11, 0, 20, 0, 0),
}
# dow: 0=Sun 1=Mon .. 6=Sat  (matches strftime('%w'))
DAY_COLS = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5), ("Sat", 6), ("Sun", 0)]
DAY_CSV = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
CSV_COLS = ["AGENT_ID", "AGENT_NAME", "LEVEL"] + DAY_CSV + ["CUSTOM_START", "CUSTOM_END"]
SHIFT_CHOICES = ["off", "M", "A", "N", "D", "E", "CUSTOM"]
SHIFT_LABELS = {"M": "Morning 05–14", "A": "Afternoon 14–23", "N": "Night 23–05 (+1d)",
                "D": "Day 08–17", "E": "11–20", "CUSTOM": "Custom window"}


# --- password gate ---------------------------------------------------------
def _gate():
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        return True
    if st.session_state.get("authed"):
        return True
    entered = st.text_input("Password", type="password")
    if entered and entered == pw:
        st.session_state["authed"] = True
        return True
    if entered:
        st.error("Incorrect password")
    return False


if not _gate():
    st.stop()


# --- Snowflake ------------------------------------------------------------
@st.cache_resource
def get_conn():
    import snowflake.connector
    snowflake.connector.paramstyle = "qmark"   # so ? binds work for roster writes
    kwargs = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"], warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"], schema=os.environ["SNOWFLAKE_SCHEMA"],
    )
    if os.environ.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = os.environ["SNOWFLAKE_ROLE"]
    return snowflake.connector.connect(**kwargs)


@st.cache_data(ttl=60)
def run_df(sql: str) -> pd.DataFrame:
    return query(sql)


def query(sql: str) -> pd.DataFrame:
    """Uncached query (used for the roster, so edits reflect immediately)."""
    cur = get_conn().cursor()
    try:
        cur.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        cur.close()
    return pd.DataFrame(rows, columns=cols)


def _hms(secs) -> str:
    secs = int(secs or 0)
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


def _ist(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


# --- Roster storage (Snowflake AGENT_ROSTER, keyed by PERIOD_START) --------
def ensure_roster_table():
    cur = get_conn().cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS AGENT_ROSTER ("
        "PERIOD_START VARCHAR, AGENT_ID VARCHAR, AGENT_NAME VARCHAR, LEVEL VARCHAR, "
        "DOW NUMBER, SHIFT_CODE VARCHAR, CUSTOM_START VARCHAR, CUSTOM_END VARCHAR, "
        "UPDATED_AT VARCHAR)")
    # Migrate an older MONTH-keyed table, if present.
    try:
        cur.execute("ALTER TABLE AGENT_ROSTER ADD COLUMN IF NOT EXISTS PERIOD_START VARCHAR")
    except Exception:
        pass
    try:
        cur.execute("UPDATE AGENT_ROSTER SET PERIOD_START = MONTH || '-01' "
                    "WHERE PERIOD_START IS NULL AND MONTH IS NOT NULL")
    except Exception:
        pass
    cur.close()


def list_periods():
    df = query("SELECT DISTINCT PERIOD_START FROM AGENT_ROSTER "
               "WHERE PERIOD_START IS NOT NULL ORDER BY PERIOD_START DESC")
    return [str(x) for x in df["PERIOD_START"].tolist()] if not df.empty else []


def active_period_for(d):
    """The roster period covering IST date d = latest PERIOD_START <= d."""
    df = query("SELECT MAX(PERIOD_START) AS M FROM AGENT_ROSTER "
               f"WHERE PERIOD_START <= '{d.isoformat()}'")
    v = df["M"][0] if not df.empty else None
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def load_roster_period(period: str):
    df = query(
        "SELECT AGENT_ID, AGENT_NAME, LEVEL, DOW, SHIFT_CODE, CUSTOM_START, CUSTOM_END "
        f"FROM AGENT_ROSTER WHERE PERIOD_START = '{period}'")
    roster = {}
    for _, r in df.iterrows():
        aid = str(r["AGENT_ID"])
        a = roster.setdefault(aid, {"name": r["AGENT_NAME"], "level": r["LEVEL"], "week": {},
                                    "cstart": _cell(r["CUSTOM_START"]), "cend": _cell(r["CUSTOM_END"])})
        code = r["SHIFT_CODE"]
        if code and code != "off":
            a["week"][int(r["DOW"])] = code
    return roster


def save_roster_period(period: str, records):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM AGENT_ROSTER WHERE PERIOD_START = ?", (period,))
        if records:
            cur.executemany(
                "INSERT INTO AGENT_ROSTER (PERIOD_START, AGENT_ID, AGENT_NAME, LEVEL, DOW, "
                "SHIFT_CODE, CUSTOM_START, CUSTOM_END, UPDATED_AT) VALUES (?,?,?,?,?,?,?,?,?)",
                records)
        conn.commit()
    finally:
        cur.close()


def _roster_records(period, rd):
    """dict roster -> per-(agent, dow) rows."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = []
    for aid, a in rd.items():
        cs = _cell(a.get("cstart")) or None
        ce = _cell(a.get("cend")) or None
        for _, dw in DAY_COLS:
            recs.append((period, aid, a["name"], a["level"], dw,
                         a["week"].get(dw, "off"), cs, ce, now))
    return recs


def _edited_to_records(period, df):
    """Wide editor grid -> per-(agent, dow) rows."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = []
    for _, r in df.iterrows():
        cs = _cell(r["Custom start"]) or None
        ce = _cell(r["Custom end"]) or None
        for label, dw in DAY_COLS:
            recs.append((period, str(r["AGENT_ID"]), r["Agent"], r["Level"], dw,
                         r[label], cs, ce, now))
    return recs


def roster_to_wide(rd):
    cols = ["AGENT_ID", "Agent", "Level", "Custom start", "Custom end"] + [l for l, _ in DAY_COLS]
    rows = [{"AGENT_ID": aid, "Agent": a["name"], "Level": a["level"],
             "Custom start": a.get("cstart", ""), "Custom end": a.get("cend", ""),
             **{label: a["week"].get(dw, "off") for label, dw in DAY_COLS}}
            for aid, a in rd.items()]
    return pd.DataFrame(rows, columns=cols)


def roster_to_csv_df(rd):
    rows = []
    for aid, a in rd.items():
        row = {"AGENT_ID": aid, "AGENT_NAME": a["name"], "LEVEL": a["level"],
               "CUSTOM_START": a.get("cstart", ""), "CUSTOM_END": a.get("cend", "")}
        for (label, dw), csvh in zip(DAY_COLS, DAY_CSV):
            row[csvh] = a["week"].get(dw, "off")
        rows.append(row)
    return pd.DataFrame(rows, columns=CSV_COLS)


def csv_to_records(period, df):
    """Parse an uploaded roster CSV into records. Returns (records, skipped)."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs, skipped = [], 0
    cols = {c.upper().strip(): c for c in df.columns}
    for _, r in df.iterrows():
        aid = _cell(r[cols["AGENT_ID"]]) if "AGENT_ID" in cols else ""
        if not aid:
            skipped += 1
            continue
        name = _cell(r[cols["AGENT_NAME"]]) if "AGENT_NAME" in cols else aid
        level = (_cell(r[cols["LEVEL"]]).upper() if "LEVEL" in cols else "L1") or "L1"
        cs = (_cell(r[cols["CUSTOM_START"]]) if "CUSTOM_START" in cols else "") or None
        ce = (_cell(r[cols["CUSTOM_END"]]) if "CUSTOM_END" in cols else "") or None
        for (label, dw), csvh in zip(DAY_COLS, DAY_CSV):
            code = (_cell(r[cols[csvh]]) if csvh in cols else "off") or "off"
            if code not in SHIFT_CHOICES:
                code = "off"
            recs.append((period, aid, name, level, dw, code, cs, ce, now))
    return recs, skipped


def _parse_hm(s):
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def shift_window(d, code, cstart, cend):
    """(start_utc_naive, end_utc_naive) for shift `code` on IST date d, or None."""
    if code == "CUSTOM":
        a, b = _parse_hm(cstart), _parse_hm(cend)
        if not a or not b:
            return None
        sh, sm = a
        eh, em = b
        eoff = 1 if (eh * 60 + em) <= (sh * 60 + sm) else 0
    elif code in SHIFTS:
        sh, sm, eh, em, eoff = SHIFTS[code]
    else:
        return None
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    end = (datetime(d.year, d.month, d.day, eh, em, tzinfo=IST) + timedelta(days=eoff)) \
        .astimezone(UTC).replace(tzinfo=None)
    return start, end


def _is_readonly(q):
    """Allow only a single read-only statement in the SQL tab."""
    s = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    s = "\n".join(ln for ln in s.splitlines() if not ln.strip().startswith("--"))
    s = s.strip().rstrip(";").strip()
    if not s:
        return False, "Empty query."
    if ";" in s:
        return False, "Only a single statement is allowed."
    first = s.split(None, 1)[0].upper()
    allowed = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
    if first not in allowed:
        return False, f"Read-only only: allowed = {', '.join(sorted(allowed))} (got '{first}')."
    return True, ""


# --- header / controls ----------------------------------------------------
st.title("Support Agent Presence")
try:
    get_conn()
    ensure_roster_table()
except Exception as exc:  # noqa: BLE001
    st.error(f"Snowflake connection failed: {exc}")
    st.stop()

top = st.columns([1, 1, 4])
if top[0].button("↻ Refresh"):
    st.cache_data.clear()
today_ist = datetime.now(IST).date()
day = top[1].date_input("Report day (IST)", today_ist - timedelta(days=1))

k1, k2 = st.columns(2)
k1.metric("Online now", int(run_df("SELECT COUNT(*) N FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL")["N"][0]))
k2.metric("Team online hrs (7d)", float(run_df(
    "SELECT COALESCE(ROUND(SUM(ONLINE_SECS)/3600.0,1),0) H FROM V_SESSIONS "
    "WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata',LOGIN_UTC) >= "
    "DATEADD('day',-7,CONVERT_TIMEZONE('UTC','Asia/Kolkata',SYSDATE()))")["H"][0]))

tab_comp, tab_pres, tab_roster, tab_sql = st.tabs(
    ["Shift compliance", "Presence", "Roster", "SQL"])

# =====================  Shift compliance  =================================
with tab_comp:
    st.subheader(f"Shift compliance — {day}")
    period = active_period_for(day)
    roster = load_roster_period(period) if period else {}

    filt = [c for c in SHIFT_CHOICES if c != "off"]
    ctl = st.columns([3, 2, 2, 2, 2])
    sel_shifts = ctl[0].multiselect(
        "Shifts to include", filt, default=filt, format_func=lambda s: SHIFT_LABELS.get(s, s))
    override = ctl[1].selectbox(
        "Evaluate against", ["Roster shift", "M", "A", "N", "D", "E"],
        format_func=lambda s: s if s == "Roster shift" else SHIFT_LABELS[s])
    late_buf = ctl[2].number_input("Late-login buffer (min)", min_value=0, value=15, step=1)
    l2_buf = ctl[3].number_input("Early-logout buffer L2 (min)", min_value=0, value=30, step=1)
    l1_buf = ctl[4].number_input("Early-logout buffer L1 (min)", min_value=0, value=0, step=1)
    break_min = st.number_input("Flag mid-shift offline over (min)", min_value=0, value=5, step=1)
    override_code = None if override == "Roster shift" else override

    dow = int(day.strftime("%w"))
    scheduled = [(aid, d, d["week"][dow]) for aid, d in roster.items()
                 if d["week"].get(dow, "off") != "off" and d["week"][dow] in sel_shifts]
    if not period or not roster:
        st.info(f"No roster covers **{day}**. Add a roster period in the **Roster** tab.")
    elif not scheduled:
        st.info(f"Nobody rostered for the selected shifts on this day (period {period}).")
    else:
        lo = (day - timedelta(days=1)).isoformat()
        hi = (day + timedelta(days=1)).isoformat()
        sess = run_df(
            f"SELECT AGENT_ID, LOGIN_UTC, LOGOUT_UTC FROM V_SESSIONS "
            f"WHERE LOGIN_UTC >= '{lo} 00:00:00' AND LOGIN_UTC < '{hi} 12:00:00'")
        by_agent = defaultdict(list)
        for _, r in sess.iterrows():
            by_agent[str(r["AGENT_ID"])].append((r["LOGIN_UTC"], r["LOGOUT_UTC"]))
        now_utc = datetime.now(UTC).replace(tzinfo=None)

        out = []
        for aid, d, code in scheduled:
            eff = override_code or code
            win_res = shift_window(day, eff, d["cstart"], d["cend"])
            if win_res is None:
                continue
            start, end = win_res
            win = (end - start).total_seconds()

            periods, has_open, last_logout = [], False, None
            for lin, lout in by_agent.get(aid, []):
                lout_eff = lout if lout is not None else now_utc
                a, b = max(lin, start), min(lout_eff, end)
                if b > a:
                    periods.append((a, b))
                    if lout is None:
                        has_open = True
                    elif last_logout is None or lout > last_logout:
                        last_logout = lout
            periods.sort()

            online = sum((b - a).total_seconds() for a, b in periods)
            first_login = periods[0][0] if periods else None
            last_online = periods[-1][1] if periods else None
            breaks, break_secs = 0, 0.0
            for i in range(1, len(periods)):
                gap = (periods[i][0] - periods[i - 1][1]).total_seconds()
                if gap > 0:
                    breaks += 1
                    break_secs += gap

            grace = (l2_buf if d["level"] == "L2" else l1_buf) * 60
            login_delay = (first_login is None) or (first_login > start + timedelta(minutes=late_buf))
            early_logout = (last_online is None) or (last_online < end - timedelta(seconds=grace))

            out.append({
                "Agent": d["name"], "Lvl": d["level"], "Shift": eff,
                "Window (IST)": f"{_ist(start)[-8:]}–{_ist(end)[-8:]}",
                "First login": "" if first_login is None else _ist(first_login)[-8:],
                "Last logout": "still online" if has_open
                               else (_ist(last_logout)[-8:] if last_logout else ""),
                "Not online": _hms(max(0, win - online)),
                "Breaks": breaks,
                "Break time": _hms(break_secs),
                "Late login": "⚠️" if login_delay else "",
                "Early logout": "⚠️" if early_logout else "",
                "Mid-shift offline": "⚠️" if break_secs > break_min * 60 else "",
            })
        df = pd.DataFrame(out).sort_values(["Lvl", "Agent"])
        m = st.columns(4)
        m[0].metric("Late logins", int((df["Late login"] == "⚠️").sum()))
        m[1].metric("Early logouts", int((df["Early logout"] == "⚠️").sum()))
        m[2].metric("Mid-shift offline", int((df["Mid-shift offline"] == "⚠️").sum()))
        m[3].metric("Absent", int((df["First login"] == "").sum()))
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"Times IST. Roster period **{period}**. Buffers: late {late_buf}m, "
                   f"early L2 {l2_buf}m / L1 {l1_buf}m. 'Evaluate against' overrides the shift window.")

# =====================  Presence  =========================================
with tab_pres:
    st.subheader("Agents online right now")
    st.dataframe(run_df(
        "SELECT AGENT_NAME, CONVERT_TIMEZONE('UTC','Asia/Kolkata',LOGIN_UTC) ONLINE_SINCE_IST, "
        "ROUND(DATEDIFF('second',LOGIN_UTC,SYSDATE())/3600.0,2) ONLINE_HOURS "
        "FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL ORDER BY LOGIN_UTC"),
        use_container_width=True, hide_index=True)
    cA, cB = st.columns(2)
    with cA:
        st.subheader("Online hours per agent (7d)")
        d = run_df(
            "SELECT AGENT_NAME, ROUND(SUM(ONLINE_SECS)/3600.0,1) ONLINE_HOURS FROM V_SESSIONS "
            "WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata',LOGIN_UTC) >= "
            "DATEADD('day',-7,CONVERT_TIMEZONE('UTC','Asia/Kolkata',SYSDATE())) "
            "GROUP BY AGENT_NAME ORDER BY ONLINE_HOURS DESC")
        if not d.empty:
            st.bar_chart(d.set_index("AGENT_NAME"))
    with cB:
        st.subheader("Team online hours / day")
        d = run_df(
            "SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata',LOGIN_UTC)) IST_DAY, "
            "ROUND(SUM(ONLINE_SECS)/3600.0,1) HRS FROM V_SESSIONS GROUP BY 1 ORDER BY 1")
        if not d.empty:
            st.line_chart(d.set_index("IST_DAY"))
    st.subheader("Current status per agent")
    st.dataframe(run_df(
        "SELECT AGENT_NAME, CURRENT_STATE, CURRENT_REASON, "
        "CONVERT_TIMEZONE('UTC','Asia/Kolkata',SINCE_UTC) SINCE_IST "
        "FROM V_CURRENT_STATUS ORDER BY AGENT_NAME"),
        use_container_width=True, hide_index=True)

# =====================  Roster  ===========================================
with tab_roster:
    st.subheader("Roster — shift periods")
    st.caption("A roster **period** starts on the date you pick (the Monday the rotation begins) "
               "and applies until the next period starts. Codes: off / M / A / N / D / E / CUSTOM "
               "(CUSTOM uses Custom start/end, HH:MM 24h IST).")

    periods = list_periods()
    if periods:
        st.caption("Existing periods: " + ", ".join(periods))
    default_start = datetime.fromisoformat(periods[0]).date() if periods else today_ist
    start = st.date_input("Roster start date", default_start,
                          help="The date this rotation begins; applies until the next period start.")
    rperiod = start.isoformat()
    existing = load_roster_period(rperiod)
    prev = next((p for p in periods if p < rperiod), None)

    b = st.columns([2, 2, 2, 3])
    if prev and b[0].button(f"Copy {prev} → {rperiod}"):
        save_roster_period(rperiod, _roster_records(rperiod, load_roster_period(prev)))
        st.session_state.pop(f"roster_ed_{rperiod}", None)
        st.rerun()
    src = existing if existing else {}
    b[1].download_button("⬇ Download CSV", roster_to_csv_df(src).to_csv(index=False),
                         file_name=f"roster_{rperiod}.csv", mime="text/csv")
    up = b[2].file_uploader("⬆ Upload CSV", type=["csv"], key=f"up_{rperiod}",
                            label_visibility="collapsed")
    if up is not None and b[3].button("Import uploaded CSV → replace period"):
        try:
            recs, skipped = csv_to_records(rperiod, pd.read_csv(up, dtype=str))
            save_roster_period(rperiod, recs)
            st.session_state.pop(f"roster_ed_{rperiod}", None)
            msg = f"Imported {len(recs)//7} agents for {rperiod}."
            if skipped:
                msg += f" Skipped {skipped} row(s) without AGENT_ID."
            st.success(msg)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"CSV import failed: {exc}")

    if not existing:
        st.warning(f"No roster saved for {rperiod} yet — empty grid. Copy a previous period, "
                   "upload a CSV, or add agents below, then Save.")

    wide = roster_to_wide(src)
    edited = st.data_editor(
        wide, use_container_width=True, hide_index=True, num_rows="fixed",
        key=f"roster_ed_{rperiod}",
        column_config={
            "AGENT_ID": None,
            "Agent": st.column_config.TextColumn("Agent", disabled=True),
            "Level": st.column_config.SelectboxColumn("Level", options=["L1", "L2"], width="small"),
            "Custom start": st.column_config.TextColumn("Custom start", width="small"),
            "Custom end": st.column_config.TextColumn("Custom end", width="small"),
            **{label: st.column_config.SelectboxColumn(label, options=SHIFT_CHOICES, width="small")
               for label, _ in DAY_COLS},
        },
    )

    if st.button("💾 Save roster", type="primary"):
        try:
            save_roster_period(rperiod, _edited_to_records(rperiod, edited))
            st.success(f"Saved roster for period {rperiod} ({len(edited)} agents).")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Save failed: {exc}")

    with st.expander("➕ ➖  Add / remove agents"):
        st.caption("Add/remove saves the current grid immediately (including any edits above).")
        existing_ids = set(edited["AGENT_ID"].astype(str)) if not edited.empty else set()
        try:
            dirdf = run_df("SELECT AGENT_ID, COALESCE(NAME, AGENT_ID) AS NAME FROM AGENTS ORDER BY NAME")
        except Exception:
            dirdf = pd.DataFrame(columns=["AGENT_ID", "NAME"])
        addable = [(str(t.AGENT_ID), str(t.NAME)) for t in dirdf.itertuples()
                   if str(t.AGENT_ID) not in existing_ids]

        ac = st.columns([4, 1])
        if addable:
            pick = ac[0].selectbox("Add agent from directory", addable,
                                   format_func=lambda t: f"{t[1]} ({t[0]})", key=f"add_{rperiod}")
            if ac[1].button("Add", key=f"addbtn_{rperiod}"):
                new_row = {"AGENT_ID": pick[0], "Agent": pick[1], "Level": "L1",
                           "Custom start": "", "Custom end": "",
                           **{label: "off" for label, _ in DAY_COLS}}
                merged = pd.concat([edited, pd.DataFrame([new_row])], ignore_index=True)
                save_roster_period(rperiod, _edited_to_records(rperiod, merged))
                st.session_state.pop(f"roster_ed_{rperiod}", None)
                st.rerun()
        else:
            ac[0].caption("Nobody left to add — all directory agents are on this period, or the "
                          "AGENTS directory is empty (run scripts/sync_agents.py).")

        rc = st.columns([4, 1])
        cur_agents = [(str(r["AGENT_ID"]), str(r["Agent"])) for _, r in edited.iterrows()]
        if cur_agents:
            rpick = rc[0].selectbox("Remove agent from this period", cur_agents,
                                    format_func=lambda t: f"{t[1]} ({t[0]})", key=f"rm_{rperiod}")
            if rc[1].button("Remove", key=f"rmbtn_{rperiod}"):
                kept = edited[edited["AGENT_ID"].astype(str) != rpick[0]]
                save_roster_period(rperiod, _edited_to_records(rperiod, kept))
                st.session_state.pop(f"roster_ed_{rperiod}", None)
                st.rerun()

# =====================  SQL (read-only)  ==================================
with tab_sql:
    st.subheader("Ad-hoc SQL (read-only)")
    st.caption("Single statement, read-only: SELECT / WITH / SHOW / DESCRIBE / EXPLAIN. "
               "Results capped at 1000 rows. Tables: STATE_EVENTS, WORK_ITEM_EVENTS, AGENTS, "
               "AGENT_ROSTER; views: V_SESSIONS, V_WORK_ITEMS, V_CURRENT_STATUS, V_STATE_EVENTS.")
    q = st.text_area("Query", "SELECT * FROM V_SESSIONS ORDER BY LOGIN_UTC DESC LIMIT 20", height=150)
    if st.button("Run query", type="primary"):
        ok, msg = _is_readonly(q)
        if not ok:
            st.error(msg)
        else:
            try:
                cur = get_conn().cursor()
                try:
                    cur.execute(q)
                    cols = [c[0] for c in cur.description] if cur.description else []
                    rows = cur.fetchmany(1000)
                finally:
                    cur.close()
                res = pd.DataFrame(rows, columns=cols)
                st.caption(f"{len(res)} row(s) (capped at 1000).")
                st.dataframe(res, use_container_width=True, hide_index=True)
                if not res.empty:
                    st.download_button("⬇ Download results CSV", res.to_csv(index=False),
                                       file_name="query_result.csv", mime="text/csv")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Query failed: {exc}")
