"""Streamlit-in-Snowflake dashboard for agent presence & workload.

Replaces the deprecated Snowsight dashboards. Runs natively in Snowflake and
queries the views created by deploy/snowflake_dashboard.sql (Part 1).

Deploy: Snowsight -> Projects -> Streamlit -> + Streamlit App. Create it in the
SAME database.schema where the V_* views live (so the unqualified names below
resolve), pick a warehouse, paste this file, Run. No extra packages needed —
streamlit, snowflake-snowpark-python and pandas are preinstalled in SiS.
"""

import streamlit as st
from snowflake.snowpark.context import get_active_session

st.set_page_config(page_title="Agent Presence", layout="wide")
session = get_active_session()


@st.cache_data(ttl=60)
def run(sql: str):
    return session.sql(sql).to_pandas()


st.title("Support Agent Presence & Workload")
st.caption("All times IST. Source: STATE_EVENTS / WORK_ITEM_EVENTS (via V_* views).")

# --- KPI row ---------------------------------------------------------------
k1, k2, k3 = st.columns(3)
online_now = run("SELECT COUNT(*) AS N FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL")
k1.metric("Online now", int(online_now["N"][0]))

team_hours = run(
    """SELECT COALESCE(ROUND(SUM(ONLINE_SECS)/3600.0, 1), 0) AS H
       FROM V_SESSIONS
       WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)
             >= DATEADD('day', -7, CONVERT_TIMEZONE('UTC','Asia/Kolkata', SYSDATE()))"""
)
k2.metric("Team online hrs (7d)", float(team_hours["H"][0]))

items_7d = run(
    "SELECT COUNT(*) AS N FROM V_WORK_ITEMS WHERE ADDED_UTC >= DATEADD('day', -7, SYSDATE())"
)
k3.metric("Items handled (7d)", int(items_7d["N"][0]))

# --- Online now ------------------------------------------------------------
st.subheader("Agents online right now")
st.dataframe(
    run(
        """SELECT AGENT_NAME,
                  CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC) AS ONLINE_SINCE_IST,
                  ROUND(DATEDIFF('second', LOGIN_UTC, SYSDATE())/3600.0, 2) AS ONLINE_HOURS
           FROM V_SESSIONS WHERE LOGOUT_UTC IS NULL ORDER BY LOGIN_UTC"""
    ),
    use_container_width=True,
)

# --- Online hours per agent (7d) ------------------------------------------
st.subheader("Online hours per agent — last 7 days")
oha = run(
    """SELECT AGENT_NAME, ROUND(SUM(ONLINE_SECS)/3600.0, 1) AS ONLINE_HOURS
       FROM V_SESSIONS
       WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)
             >= DATEADD('day', -7, CONVERT_TIMEZONE('UTC','Asia/Kolkata', SYSDATE()))
       GROUP BY AGENT_NAME ORDER BY ONLINE_HOURS DESC"""
)
if not oha.empty:
    st.bar_chart(oha.set_index("AGENT_NAME"))

# --- Trends ----------------------------------------------------------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("Team online hours / day")
    d = run(
        """SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)) AS IST_DAY,
                  ROUND(SUM(ONLINE_SECS)/3600.0, 1) AS TEAM_ONLINE_HOURS
           FROM V_SESSIONS GROUP BY 1 ORDER BY 1"""
    )
    if not d.empty:
        st.line_chart(d.set_index("IST_DAY"))
with c2:
    st.subheader("Avg handle time (min) / day")
    d = run(
        """SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', ADDED_UTC)) AS IST_DAY,
                  ROUND(AVG(HANDLE_SECS)/60.0, 1) AS AVG_HANDLE_MIN
           FROM V_WORK_ITEMS WHERE HANDLE_SECS IS NOT NULL GROUP BY 1 ORDER BY 1"""
    )
    if not d.empty:
        st.line_chart(d.set_index("IST_DAY"))

# --- Work items ------------------------------------------------------------
st.subheader("Work items added vs completed / day")
d = run(
    """SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', ADDED_UTC)) AS IST_DAY,
              COUNT(*) AS ITEMS_ADDED, COUNT(REMOVED_UTC) AS ITEMS_COMPLETED
       FROM V_WORK_ITEMS WHERE ADDED_UTC IS NOT NULL GROUP BY 1 ORDER BY 1"""
)
if not d.empty:
    st.bar_chart(d.set_index("IST_DAY"))

# --- Current status + channel mix -----------------------------------------
c3, c4 = st.columns(2)
with c3:
    st.subheader("Current status per agent")
    st.dataframe(
        run(
            """SELECT AGENT_NAME, CURRENT_STATE, CURRENT_REASON,
                      CONVERT_TIMEZONE('UTC','Asia/Kolkata', SINCE_UTC) AS SINCE_IST
               FROM V_CURRENT_STATUS ORDER BY AGENT_NAME"""
        ),
        use_container_width=True,
    )
with c4:
    st.subheader("Items by channel (7d)")
    d = run(
        """SELECT COALESCE(CHANNEL,'unknown') AS CHANNEL, COUNT(*) AS ITEMS
           FROM V_WORK_ITEMS WHERE ADDED_UTC >= DATEADD('day', -7, SYSDATE())
           GROUP BY 1 ORDER BY ITEMS DESC"""
    )
    if not d.empty:
        st.bar_chart(d.set_index("CHANNEL"))
