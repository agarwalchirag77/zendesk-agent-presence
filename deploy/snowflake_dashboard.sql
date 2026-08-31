-- ============================================================================
-- Snowflake views + dashboard tile queries for the agent-presence data.
--
-- The sink stores only the RAW append-only events; these views reconstruct
-- sessions / work-item spans / current status from them (the same logic the
-- SQLite app applies). Timestamps are stored as ISO strings, so we trim to
-- seconds and parse to TIMESTAMP_NTZ (UTC). IST is applied in the tile queries.
--
-- HOW TO USE:
--   1) In a Snowsight worksheet, set context, then RUN ALL (creates the views):
--        USE ROLE <your_role>;
--        USE WAREHOUSE <your_wh>;
--        USE SCHEMA <your_db>.<your_schema>;
--      -- then run everything in PART 1 below.
--   2) Projects -> Dashboards -> + Dashboard. Add a tile per query in PART 2:
--      New tile -> paste query -> Run -> pick a chart type -> Return to dashboard.
-- ============================================================================

-- ############################  PART 1: VIEWS  ###############################

-- Parsed state events (UTC) with agent display name.
CREATE OR REPLACE VIEW V_STATE_EVENTS AS
SELECT
  se.EVENT_ID,
  se.AGENT_ID,
  COALESCE(a.NAME, se.AGENT_ID)                                   AS AGENT_NAME,
  se.NEW_STATE_NAME,
  se.NEW_REASON,
  (LOWER(se.NEW_STATE_NAME) = 'offline')                          AS IS_OFFLINE,
  TRY_TO_TIMESTAMP_NTZ(REPLACE(SUBSTR(se.STATE_UPDATED_AT,1,19),'T',' '),
                       'YYYY-MM-DD HH24:MI:SS')                   AS TS_UTC
FROM STATE_EVENTS se
LEFT JOIN AGENTS a ON a.AGENT_ID = se.AGENT_ID;

-- Presence sessions: a session spans from an offline->non-offline transition
-- (login) to the next transition into offline (logout). online->away->online
-- stays a single session. LOGOUT_UTC NULL = still online.
CREATE OR REPLACE VIEW V_SESSIONS AS
WITH base AS (
  SELECT AGENT_ID, AGENT_NAME, TS_UTC, IS_OFFLINE,
         LAG(IS_OFFLINE) OVER (PARTITION BY AGENT_ID ORDER BY TS_UTC) AS PREV_OFF
  FROM V_STATE_EVENTS
  WHERE TS_UTC IS NOT NULL
),
logins AS (
  SELECT AGENT_ID, AGENT_NAME, TS_UTC AS LOGIN_UTC
  FROM base
  WHERE IS_OFFLINE = FALSE AND (PREV_OFF = TRUE OR PREV_OFF IS NULL)
),
offs AS (
  SELECT AGENT_ID, TS_UTC AS OFF_UTC FROM base WHERE IS_OFFLINE = TRUE
)
SELECT
  l.AGENT_ID,
  l.AGENT_NAME,
  l.LOGIN_UTC,
  MIN(o.OFF_UTC)                                                    AS LOGOUT_UTC,
  DATEDIFF('second', l.LOGIN_UTC, COALESCE(MIN(o.OFF_UTC), SYSDATE())) AS ONLINE_SECS
FROM logins l
LEFT JOIN offs o ON o.AGENT_ID = l.AGENT_ID AND o.OFF_UTC > l.LOGIN_UTC
GROUP BY l.AGENT_ID, l.AGENT_NAME, l.LOGIN_UTC;

-- Latest known state per agent.
CREATE OR REPLACE VIEW V_CURRENT_STATUS AS
SELECT AGENT_ID, AGENT_NAME, NEW_STATE_NAME AS CURRENT_STATE,
       NEW_REASON AS CURRENT_REASON, TS_UTC AS SINCE_UTC
FROM (
  SELECT v.*, ROW_NUMBER() OVER (PARTITION BY AGENT_ID ORDER BY TS_UTC DESC) AS RN
  FROM V_STATE_EVENTS v WHERE TS_UTC IS NOT NULL
)
WHERE RN = 1;

-- Work-item spans: added -> removed per (agent, work item), with handle time.
CREATE OR REPLACE VIEW V_WORK_ITEMS AS
WITH wi AS (
  SELECT AGENT_ID, WORK_ITEM_ID, CHANNEL, KIND, REASON, PREVIOUS_REASON,
         TRY_TO_TIMESTAMP_NTZ(REPLACE(SUBSTR(ITEM_UPDATED_AT,1,19),'T',' '),
                              'YYYY-MM-DD HH24:MI:SS') AS TS_UTC
  FROM WORK_ITEM_EVENTS
)
SELECT
  w.AGENT_ID,
  COALESCE(a.NAME, w.AGENT_ID)                                     AS AGENT_NAME,
  w.WORK_ITEM_ID,
  ANY_VALUE(w.CHANNEL)                                            AS CHANNEL,
  MIN(CASE WHEN w.KIND = 'added'   THEN w.TS_UTC END)             AS ADDED_UTC,
  MAX(CASE WHEN w.KIND = 'removed' THEN w.TS_UTC END)             AS REMOVED_UTC,
  MAX(CASE WHEN UPPER(w.REASON) = 'ACCEPTED'
            OR UPPER(w.PREVIOUS_REASON) = 'ACCEPTED' THEN 1 ELSE 0 END) AS ACCEPTED,
  DATEDIFF('second',
           MIN(CASE WHEN w.KIND = 'added'   THEN w.TS_UTC END),
           MAX(CASE WHEN w.KIND = 'removed' THEN w.TS_UTC END))   AS HANDLE_SECS
FROM wi w
LEFT JOIN AGENTS a ON a.AGENT_ID = w.AGENT_ID
GROUP BY w.AGENT_ID, a.NAME, w.WORK_ITEM_ID;


-- #######################  PART 2: DASHBOARD TILES  ##########################
-- Paste each block as its own Snowsight tile. Suggested chart type in comments.

-- Tile 1 — Agents online right now  (chart: Table, or Scorecard on COUNT)
SELECT AGENT_NAME,
       CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)          AS ONLINE_SINCE_IST,
       ROUND(DATEDIFF('second', LOGIN_UTC, SYSDATE())/3600.0, 2)  AS ONLINE_HOURS
FROM V_SESSIONS
WHERE LOGOUT_UTC IS NULL
ORDER BY LOGIN_UTC;

-- Tile 2 — Online hours per agent, last 7 IST days  (chart: Bar)
SELECT AGENT_NAME, ROUND(SUM(ONLINE_SECS)/3600.0, 1) AS ONLINE_HOURS
FROM V_SESSIONS
WHERE CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)
      >= DATEADD('day', -7, CONVERT_TIMEZONE('UTC','Asia/Kolkata', SYSDATE()))
GROUP BY AGENT_NAME
ORDER BY ONLINE_HOURS DESC;

-- Tile 3 — Team online hours per IST day  (chart: Line)
SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', LOGIN_UTC)) AS IST_DAY,
       ROUND(SUM(ONLINE_SECS)/3600.0, 1)                          AS TEAM_ONLINE_HOURS
FROM V_SESSIONS
GROUP BY 1 ORDER BY 1;

-- Tile 4 — Work items added vs completed per IST day  (chart: Bar, 2 series)
SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', ADDED_UTC)) AS IST_DAY,
       COUNT(*)               AS ITEMS_ADDED,
       COUNT(REMOVED_UTC)     AS ITEMS_COMPLETED
FROM V_WORK_ITEMS
WHERE ADDED_UTC IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- Tile 5 — Avg handle time (minutes) per IST day  (chart: Line)
SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', ADDED_UTC)) AS IST_DAY,
       ROUND(AVG(HANDLE_SECS)/60.0, 1)                            AS AVG_HANDLE_MIN
FROM V_WORK_ITEMS
WHERE HANDLE_SECS IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- Tile 6 — Current status per agent  (chart: Table)
SELECT AGENT_NAME, CURRENT_STATE, CURRENT_REASON,
       CONVERT_TIMEZONE('UTC','Asia/Kolkata', SINCE_UTC)          AS SINCE_IST
FROM V_CURRENT_STATUS
ORDER BY AGENT_NAME;

-- Tile 7 — Items by channel, last 7 days  (chart: Bar or Pie)
SELECT COALESCE(CHANNEL,'unknown') AS CHANNEL, COUNT(*) AS ITEMS
FROM V_WORK_ITEMS
WHERE ADDED_UTC >= DATEADD('day', -7, SYSDATE())
GROUP BY 1 ORDER BY ITEMS DESC;

-- Tile 8 — Event volume per IST day (sanity / ingestion health)  (chart: Line)
SELECT TO_DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', TS_UTC))    AS IST_DAY,
       COUNT(*)                                                   AS STATE_EVENTS
FROM V_STATE_EVENTS
WHERE TS_UTC IS NOT NULL
GROUP BY 1 ORDER BY 1;
