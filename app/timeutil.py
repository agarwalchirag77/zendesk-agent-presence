"""IST (Asia/Kolkata, UTC+5:30) helpers for the daily reports.

Storage stays UTC everywhere; only the consumer-facing reports render and
bucket in IST, since the people reading them are in India.
"""

from datetime import date, datetime, timedelta, timezone

try:  # prefer the real tz database (handles any future rule changes)
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback if tzdata is unavailable
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

UTC = timezone.utc

from .events import _parse_iso  # reuse nanosecond-tolerant ISO parser


def ist_day_bounds(date_str: str):
    """Return (utc_start, utc_end) datetimes for the IST calendar day `date_str`.

    An IST day 00:00–24:00 maps to UTC 18:30 (prev day) – 18:30.
    """
    d = date.fromisoformat(date_str)
    start_ist = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


def to_ist(utc_iso):
    """Render a stored UTC ISO timestamp as an IST ISO string (seconds precision).

    Returns None if the input is empty/unparseable.
    """
    if not utc_iso:
        return None
    try:
        dt = _parse_iso(utc_iso)
    except (ValueError, TypeError):
        return None
    if dt is None:
        return None
    return dt.astimezone(IST).isoformat(timespec="seconds")


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def yesterday_ist() -> str:
    return (datetime.now(IST).date() - timedelta(days=1)).isoformat()
