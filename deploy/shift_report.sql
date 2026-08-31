-- ============================================================================
-- Shift-compliance report (per person, per day) from agent_sessions.
-- Run in sqlite-web / any SQLite editor against presence.db. No tables/views.
--
-- Per rostered person/day:
--   not_online_secs / not_online_hms : time within the shift the person was
--                                      OFFLINE (away/disconnected count as online)
--   login_delay  (0/1) : first login later than shift start + 15 min (or never)
--   early_logout (0/1) : last-online moment before shift end - 30 min (L2),
--                        or before shift end at all (L1)
--
-- Shifts (IST):  M 05:00-14:00 | A 14:00-23:00 | N 23:00-05:00(+1)
--                D 08:00-17:00 (= "8-5"/"DAY")  | E 11:00-20:00 (= "11-8")
--
-- The roster is the weekly Mon-Sun pattern below, keyed by agent_id and
-- automatically expanded across every June date via strftime('%w').
-- dow: 0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat.  Off days = no row.
-- Edit the date range in the `days` CTE for other months.
-- ============================================================================
WITH RECURSIVE
roster_week(agent_id, name, level, dow, shift) AS (
  VALUES
  -- ---- L1 ----
  ('53822311261721','Sasidharan M','L1',1,'N'),('53822311261721','Sasidharan M','L1',2,'N'),
  ('53822311261721','Sasidharan M','L1',3,'N'),('53822311261721','Sasidharan M','L1',4,'N'),
  ('53822311261721','Sasidharan M','L1',5,'N'),('53822311261721','Sasidharan M','L1',6,'N'),
  ('56297164605209','Tanisha Nigam','L1',1,'A'),('56297164605209','Tanisha Nigam','L1',2,'A'),
  ('56297164605209','Tanisha Nigam','L1',3,'A'),('56297164605209','Tanisha Nigam','L1',4,'A'),
  ('56297164605209','Tanisha Nigam','L1',5,'A'),('56297164605209','Tanisha Nigam','L1',0,'M'),
  ('55841011882137','Sakshee Yande','L1',1,'E'),('55841011882137','Sakshee Yande','L1',2,'E'),
  ('55841011882137','Sakshee Yande','L1',3,'E'),('55841011882137','Sakshee Yande','L1',4,'E'),
  ('55841011882137','Sakshee Yande','L1',5,'E'),
  ('57608866264089','Suhani Korde','L1',1,'E'),('57608866264089','Suhani Korde','L1',2,'E'),
  ('57608866264089','Suhani Korde','L1',3,'E'),('57608866264089','Suhani Korde','L1',4,'E'),
  ('57608866264089','Suhani Korde','L1',5,'E'),
  ('55841011742873','Vaishnavi Deshmukh','L1',1,'D'),('55841011742873','Vaishnavi Deshmukh','L1',2,'D'),
  ('55841011742873','Vaishnavi Deshmukh','L1',3,'D'),('55841011742873','Vaishnavi Deshmukh','L1',4,'D'),
  ('55841011742873','Vaishnavi Deshmukh','L1',5,'D'),
  ('57608919901337','Prajwal Gaikwad','L1',1,'E'),('57608919901337','Prajwal Gaikwad','L1',2,'E'),
  ('57608919901337','Prajwal Gaikwad','L1',3,'E'),('57608919901337','Prajwal Gaikwad','L1',4,'E'),
  ('57608919901337','Prajwal Gaikwad','L1',5,'E'),
  ('57608892303641','Ashish Rodi','L1',1,'E'),('57608892303641','Ashish Rodi','L1',2,'E'),
  ('57608892303641','Ashish Rodi','L1',3,'E'),('57608892303641','Ashish Rodi','L1',4,'E'),
  ('57608892303641','Ashish Rodi','L1',5,'E'),
  ('57608773195417','Piyush Bhamare','L1',1,'D'),('57608773195417','Piyush Bhamare','L1',2,'D'),
  ('57608773195417','Piyush Bhamare','L1',3,'D'),('57608773195417','Piyush Bhamare','L1',4,'D'),
  ('57608773195417','Piyush Bhamare','L1',5,'D'),
  ('39609874152601','Vijaysree Kalvakolanu','L1',1,'M'),('39609874152601','Vijaysree Kalvakolanu','L1',2,'M'),
  ('39609874152601','Vijaysree Kalvakolanu','L1',3,'M'),('39609874152601','Vijaysree Kalvakolanu','L1',4,'M'),
  ('39609874152601','Vijaysree Kalvakolanu','L1',5,'M'),('39609874152601','Vijaysree Kalvakolanu','L1',6,'M'),
  ('42591271083289','Jashmitha CG','L1',1,'A'),('42591271083289','Jashmitha CG','L1',2,'A'),
  ('42591271083289','Jashmitha CG','L1',3,'A'),('42591271083289','Jashmitha CG','L1',4,'A'),
  ('42591271083289','Jashmitha CG','L1',5,'A'),('42591271083289','Jashmitha CG','L1',0,'A'),
  ('34484938137241','Sthitapragyan Rout','L1',1,'N'),('34484938137241','Sthitapragyan Rout','L1',2,'N'),
  ('34484938137241','Sthitapragyan Rout','L1',3,'N'),('34484938137241','Sthitapragyan Rout','L1',4,'N'),
  ('34484938137241','Sthitapragyan Rout','L1',5,'N'),('34484938137241','Sthitapragyan Rout','L1',0,'N'),
  -- ---- L2 ----
  ('6965551948441','Veeresh Biradar','L2',1,'A'),('6965551948441','Veeresh Biradar','L2',2,'A'),
  ('6965551948441','Veeresh Biradar','L2',3,'A'),('6965551948441','Veeresh Biradar','L2',4,'A'),
  ('6965551948441','Veeresh Biradar','L2',5,'A'),
  ('6965393434137','Dimple MK','L2',1,'A'),('6965393434137','Dimple MK','L2',2,'A'),
  ('6965393434137','Dimple MK','L2',3,'A'),('6965393434137','Dimple MK','L2',4,'A'),
  ('6965393434137','Dimple MK','L2',5,'A'),
  ('6965484602009','Sudhanshu Sharan','L2',1,'D'),('6965484602009','Sudhanshu Sharan','L2',2,'D'),
  ('6965484602009','Sudhanshu Sharan','L2',3,'D'),('6965484602009','Sudhanshu Sharan','L2',4,'D'),
  ('6965484602009','Sudhanshu Sharan','L2',5,'D'),
  ('33221615443225','Nishant Tandon','L2',1,'N'),('33221615443225','Nishant Tandon','L2',2,'N'),
  ('33221615443225','Nishant Tandon','L2',3,'N'),('33221615443225','Nishant Tandon','L2',4,'N'),
  ('33221615443225','Nishant Tandon','L2',5,'N'),
  ('47133247967257','SIddhartha chauhan','L2',1,'A'),('47133247967257','SIddhartha chauhan','L2',2,'A'),
  ('47133247967257','SIddhartha chauhan','L2',3,'A'),('47133247967257','SIddhartha chauhan','L2',4,'A'),
  ('47133247967257','SIddhartha chauhan','L2',5,'A'),
  ('23621692069913','Anmol Baunthiyal','L2',1,'A'),('23621692069913','Anmol Baunthiyal','L2',2,'A'),
  ('23621692069913','Anmol Baunthiyal','L2',3,'A'),('23621692069913','Anmol Baunthiyal','L2',4,'A'),
  ('23621692069913','Anmol Baunthiyal','L2',5,'A'),
  ('39609874165017','Parthiv Patel','L2',1,'M'),('39609874165017','Parthiv Patel','L2',2,'M'),
  ('39609874165017','Parthiv Patel','L2',3,'M'),('39609874165017','Parthiv Patel','L2',4,'M'),
  ('39609874165017','Parthiv Patel','L2',5,'M'),
  ('46957151120025','Bhuvana K','L2',1,'N'),('46957151120025','Bhuvana K','L2',2,'N'),
  ('46957151120025','Bhuvana K','L2',3,'N'),('46957151120025','Bhuvana K','L2',4,'N'),
  ('46957151120025','Bhuvana K','L2',5,'N'),
  ('34470692944665','Harmanjot Kaur','L2',1,'N'),('34470692944665','Harmanjot Kaur','L2',2,'N'),
  ('34470692944665','Harmanjot Kaur','L2',3,'N'),('34470692944665','Harmanjot Kaur','L2',4,'N'),
  ('34470692944665','Harmanjot Kaur','L2',5,'N'),
  ('34484879025817','Khushi Singh','L2',1,'M'),('34484879025817','Khushi Singh','L2',2,'M'),
  ('34484879025817','Khushi Singh','L2',3,'M'),('34484879025817','Khushi Singh','L2',4,'M'),
  ('34484879025817','Khushi Singh','L2',5,'M'),('34484879025817','Khushi Singh','L2',6,'A'),
  ('53801214348697','Sameer Ramteke','L2',1,'M'),('53801214348697','Sameer Ramteke','L2',2,'M'),
  ('53801214348697','Sameer Ramteke','L2',3,'M'),('53801214348697','Sameer Ramteke','L2',4,'M'),
  ('53801214348697','Sameer Ramteke','L2',5,'M')
),

shiftdef(shift, sh, sm, eh, em, eoff) AS (
  VALUES ('M',5,0,14,0,0), ('A',14,0,23,0,0), ('N',23,0,5,0,1), ('D',8,0,17,0,0), ('E',11,0,20,0,0)
),

-- Date series for the month (edit start/end for other months).
days(day) AS (
  VALUES ('2026-06-01')
  UNION ALL
  SELECT date(day, '+1 day') FROM days WHERE day < '2026-06-30'
),

-- Person x day -> shift window as UTC julian days (IST window minus 5.5h).
sched AS (
  SELECT rw.agent_id, rw.name, rw.level, d.day, rw.shift,
         julianday(d.day)                      + (sd.sh*60+sd.sm)/1440.0 - 5.5/24.0 AS start_jd,
         julianday(d.day, '+'||sd.eoff||' day') + (sd.eh*60+sd.em)/1440.0 - 5.5/24.0 AS end_jd
  FROM days d
  JOIN roster_week rw ON rw.dow = CAST(strftime('%w', d.day) AS INTEGER)
  JOIN shiftdef sd ON sd.shift = rw.shift
),

-- Sessions, timestamps normalized to UTC julian days (nanoseconds trimmed).
sess AS (
  SELECT s.agent_id AS agent_id,
         julianday(replace(substr(s.login_at,1,19),'T',' ')) AS lin,
         julianday(replace(substr(s.logout_at,1,19),'T',' ')) AS lout_raw,            -- NULL when still open
         COALESCE(julianday(replace(substr(s.logout_at,1,19),'T',' ')), julianday('now')) AS lout
  FROM agent_sessions s
),

ov AS (
  SELECT sc.agent_id, sc.name, sc.level, sc.day, sc.shift, sc.start_jd, sc.end_jd,
         se.lin AS lin,
         se.lout_raw AS lout_raw,
         CASE WHEN se.lin IS NULL THEN NULL
              ELSE MIN(COALESCE(se.lout, sc.end_jd), sc.end_jd) END AS online_end,
         CASE WHEN se.lin IS NULL THEN 0.0
              ELSE MAX(0.0, MIN(se.lout, sc.end_jd) - MAX(se.lin, sc.start_jd)) END AS ov_days
  FROM sched sc
  LEFT JOIN sess se
    ON se.agent_id = sc.agent_id
   AND se.lin  < sc.end_jd
   AND se.lout > sc.start_jd
),

agg AS (
  SELECT agent_id, name, level, day, shift, start_jd, end_jd,
         (end_jd - start_jd) * 86400      AS shift_secs,
         COALESCE(SUM(ov_days),0) * 86400 AS online_secs,
         MIN(lin)                         AS first_login_jd,
         MAX(online_end)                  AS last_online_jd,
         MAX(CASE WHEN lin IS NOT NULL AND lout_raw IS NULL THEN 1 ELSE 0 END) AS has_open,
         MAX(lout_raw)                    AS last_logout_jd
  FROM ov
  GROUP BY agent_id, name, level, day, shift, start_jd, end_jd
),

fin AS (
  SELECT *, MAX(0, CAST(shift_secs - online_secs AS INTEGER)) AS not_online_secs
  FROM agg
)

SELECT
  name, level, day, shift,
  datetime(start_jd + 5.5/24.0)                     AS shift_start_ist,
  datetime(end_jd   + 5.5/24.0)                     AS shift_end_ist,
  CASE WHEN first_login_jd IS NULL THEN NULL
       ELSE datetime(first_login_jd + 5.5/24.0) END AS first_login_ist,
  -- Actual logout: real logout_at of the last session, or 'still online' if the
  -- last session is open, or NULL if the person never logged in that day.
  CASE WHEN has_open = 1 THEN 'still online'
       WHEN last_logout_jd IS NULL THEN NULL
       ELSE datetime(last_logout_jd + 5.5/24.0) END AS last_logout_ist,
  not_online_secs,
  printf('%02d:%02d:%02d',
         not_online_secs/3600, (not_online_secs%3600)/60, not_online_secs%60) AS not_online_hms,
  CASE WHEN first_login_jd IS NULL
            OR first_login_jd > start_jd + 15/1440.0
       THEN 1 ELSE 0 END                            AS login_delay,
  CASE WHEN last_online_jd IS NULL
            OR last_online_jd < end_jd - (CASE level WHEN 'L2' THEN 30/1440.0 ELSE 0 END)
       THEN 1 ELSE 0 END                            AS early_logout
FROM fin
-- WHERE day = '2026-06-19'      -- uncomment to scope a single day
ORDER BY day, level, name;
