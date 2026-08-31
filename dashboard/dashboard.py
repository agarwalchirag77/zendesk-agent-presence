"""Self-hosted Streamlit dashboard (runs on the EC2, reads Snowflake).

Serves the presence + workload + shift-compliance reports over a browser port so
viewers don't need SSH/pem access. Connects to Snowflake with the same
SNOWFLAKE_* env vars the app's sink uses, and reads the V_* views created by
deploy/snowflake_dashboard.sql (Part 1).

Run locally:   streamlit run dashboard/dashboard.py
On the EC2 it runs as the `zendesk-dashboard` systemd service (see
deploy/dashboard_setup.sh), bound to 0.0.0.0:8501.

Access control:
  - Set DASHBOARD_PASSWORD to gate the page (recommended).
  - Restrict inbound :8501 in the EC2 security group to your internal/VPN CIDR.
"""

import os
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

# --- Roster (keyed by agent_id) -------------------------------------------
# Shift windows (IST): start h,m  end h,m  end-day-offset.
SHIFTS = {
    "M": (5, 0, 14, 0, 0), "A": (14, 0, 23, 0, 0), "N": (23, 0, 5, 0, 1),
    "D": (8, 0, 17, 0, 0), "E": (11, 0, 20, 0, 0),
}
# dow: 0=Sun 1=Mon .. 6=Sat  (matches strftime('%w'))
def _wk(shift, days):
    return {d: shift for d in days}

ROSTER = {
    # ---- L1 ----
    "53822311261721": ("Sasidharan M", "L1", _wk("N", [1, 2, 3, 4, 5, 6])),
    "56297164605209": ("Tanisha Nigam", "L1", {**_wk("A", [1, 2, 3, 4, 5]), 0: "M"}),
    "55841011882137": ("Sakshee Yande", "L1", _wk("E", [1, 2, 3, 4, 5])),
    "57608866264089": ("Suhani Korde", "L1", _wk("E", [1, 2, 3, 4, 5])),
    "55841011742873": ("Vaishnavi Deshmukh", "L1", _wk("D", [1, 2, 3, 4, 5])),
    "57608919901337": ("Prajwal Gaikwad", "L1", _wk("E", [1, 2, 3, 4, 5])),
    "57608892303641": ("Ashish Rodi", "L1", _wk("E", [1, 2, 3, 4, 5])),
    "57608773195417": ("Piyush Bhamare", "L1", _wk("D", [1, 2, 3, 4, 5])),
    "39609874152601": ("Vijaysree Kalvakolanu", "L1", _wk("M", [1, 2, 3, 4, 5, 6])),
    "42591271083289": ("Jashmitha CG", "L1", {**_wk("A", [1, 2, 3, 4, 5]), 0: "A"}),
    "34484938137241": ("Sthitapragyan Rout", "L1", {**_wk("N", [1, 2, 3, 4, 5]), 0: "N"}),
    # ---- L2 ----
    "6965551948441": ("Veeresh Biradar", "L2", _wk("A", [1, 2, 3, 4, 5])),
    "6965393434137": ("Dimple MK", "L2", _wk("A", [1, 2, 3, 4, 5])),
    "6965484602009": ("Sudhanshu Sharan", "L2", _wk("D", [1, 2, 3, 4, 5])),
    "33221615443225": ("Nishant Tandon", "L2", _wk("N", [1, 2, 3, 4, 5])),
    "47133247967257": ("SIddhartha chauhan", "L2", _wk("A", [1, 2, 3, 4, 5])),
    "23621692069913": ("Anmol Baunthiyal", "L2", _wk("A", [1, 2, 3, 4, 5])),
    "39609874165017": ("Parthiv Patel", "L2", _wk("M", [1, 2, 3, 4, 5])),
    "46957151120025": ("Bhuvana K", "L2", _wk("N", [1, 2, 3, 4, 5])),
    "34470692944665": ("Harmanjot Kaur", "L2", _wk("N", [1, 2, 3, 4, 5])),
    "34484879025817": ("Khushi Singh", "L2", {**_wk("M", [1, 2, 3, 4, 5]), 6: "A"}),
    "53801214348697": ("Sameer Ramteke", "L2", _wk("M", [1, 2, 3, 4, 5])),
}


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
    cur = get_conn().cursor()
    try:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
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


# --- header / controls ----------------------------------------------------
st.title("Support Agent Presence & Workload")
try:
    get_conn()
except Exception as exc:  # noqa: BLE001
    st.error(f"Snowflake connection failed: {exc}")
    st.stop()

top = st.columns([1, 1, 4])
if top[0].button("↻ Refresh"):
    st.cache_data.clear()
today_ist = datetime.now(IST).date()
day = top[1].date_input("Shift report day (IST)", today_ist - timedelta(days=1))
st.caption("All times IST. Source: Snowflake V_* views over STATE_EVENTS / WORK_ITEM_EVENTS.")

# --- KPIs -----------------------------------------------------------------
k1, k2, k3 = st.columns(3)
k1.metric("Online now", int(run_df("SELECT COUNT(*) N FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL")["N"][0]))
k2.metric("Team online hrs (7d)", float(run_df(
    "SELECT COALESCE(ROUND(SUM(ONLINE_SECS)/3600.0,1),0) H FROM V_SESSIONS "
    "WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata',LOGIN_UTC) >= "
    "DATEADD('day',-7,CONVERT_TIMEZONE('UTC','Asia/Kolkata',SYSDATE()))")["H"][0]))
k3.metric("Items handled (7d)", int(run_df(
    "SELECT COUNT(*) N FROM V_WORK_ITEMS WHERE ADDED_UTC >= DATEADD('day',-7,SYSDATE())")["N"][0]))

tab1, tab2, tab3 = st.tabs(["Shift compliance", "Presence", "Workload"])

# --- Shift compliance (computed in Python from V_SESSIONS) ----------------
with tab1:
    st.subheader(f"Shift compliance — {day}")
    dow = int(day.strftime("%w"))
    scheduled = [(aid, info) for aid, info in ROSTER.items() if dow in info[2]]
    if not scheduled:
        st.info("Nobody rostered on this day.")
    else:
        lo = (day - timedelta(days=1)).isoformat()
        hi = (day + timedelta(days=1)).isoformat()
        sess = run_df(
            f"SELECT AGENT_ID, LOGIN_UTC, LOGOUT_UTC FROM V_SESSIONS "
            f"WHERE LOGIN_UTC >= '{lo} 00:00:00' AND LOGIN_UTC < '{hi} 12:00:00'"
        )
        by_agent = defaultdict(list)
        for _, r in sess.iterrows():
            by_agent[str(r["AGENT_ID"])].append((r["LOGIN_UTC"], r["LOGOUT_UTC"]))
        now_utc = datetime.now(UTC).replace(tzinfo=None)

        out = []
        for aid, (name, level, week) in scheduled:
            sh, sm, eh, em, eoff = SHIFTS[week[dow]]
            start = (datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
                     .astimezone(UTC).replace(tzinfo=None))
            end = ((datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
                    + timedelta(days=eoff)).astimezone(UTC).replace(tzinfo=None))
            win = (end - start).total_seconds()
            online = 0.0
            first_login = last_online = last_logout = None
            has_open = False
            for lin, lout in by_agent.get(aid, []):
                lout_eff = lout if lout is not None else now_utc
                ov = min(lout_eff, end) - max(lin, start)
                if ov.total_seconds() > 0:
                    online += ov.total_seconds()
                    if first_login is None or lin < first_login:
                        first_login = lin
                    oe = min(lout_eff, end)
                    if last_online is None or oe > last_online:
                        last_online = oe
                    if lout is None:
                        has_open = True
                    elif last_logout is None or lout > last_logout:
                        last_logout = lout
            grace = 30 * 60 if level == "L2" else 0
            login_delay = first_login is None or first_login > start + timedelta(minutes=15)
            early_logout = last_online is None or last_online < end - timedelta(seconds=grace)
            out.append({
                "Agent": name, "Lvl": level, "Shift": week[dow],
                "Window (IST)": f"{_ist(start)[-8:]}–{_ist(end)[-8:]}",
                "First login": _ist(first_login),
                "Last logout": "still online" if has_open else _ist(last_logout),
                "Not online": _hms(max(0, win - online)),
                "Late login": "⚠️" if login_delay else "",
                "Early logout": "⚠️" if early_logout else "",
            })
        df = pd.DataFrame(out).sort_values(["Lvl", "Agent"])
        c = st.columns(3)
        c[0].metric("Late logins", int((df["Late login"] == "⚠️").sum()))
        c[1].metric("Early logouts", int((df["Early logout"] == "⚠️").sum()))
        c[2].metric("Absent", int((df["First login"] == "").sum()))
        st.dataframe(df, use_container_width=True, hide_index=True)

# --- Presence -------------------------------------------------------------
with tab2:
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

# --- Workload -------------------------------------------------------------
with tab3:
    st.subheader("Work items added vs completed / day")
    d = run_df(
        "SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata',ADDED_UTC)) IST_DAY, "
        "COUNT(*) ITEMS_ADDED, COUNT(REMOVED_UTC) ITEMS_COMPLETED FROM V_WORK_ITEMS "
        "WHERE ADDED_UTC IS NOT NULL GROUP BY 1 ORDER BY 1")
    if not d.empty:
        st.bar_chart(d.set_index("IST_DAY"))
    cC, cD = st.columns(2)
    with cC:
        st.subheader("Avg handle time (min) / day")
        d = run_df(
            "SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata',ADDED_UTC)) IST_DAY, "
            "ROUND(AVG(HANDLE_SECS)/60.0,1) AVG_MIN FROM V_WORK_ITEMS "
            "WHERE HANDLE_SECS IS NOT NULL GROUP BY 1 ORDER BY 1")
        if not d.empty:
            st.line_chart(d.set_index("IST_DAY"))
    with cD:
        st.subheader("Items by channel (7d)")
        d = run_df(
            "SELECT COALESCE(CHANNEL,'unknown') CHANNEL, COUNT(*) ITEMS FROM V_WORK_ITEMS "
            "WHERE ADDED_UTC >= DATEADD('day',-7,SYSDATE()) GROUP BY 1 ORDER BY ITEMS DESC")
        if not d.empty:
            st.bar_chart(d.set_index("CHANNEL"))
