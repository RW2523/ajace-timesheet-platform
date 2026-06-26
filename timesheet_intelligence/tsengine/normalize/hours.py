"""Hours parsing across the many encodings seen in real timesheets.

Handles: decimals (``7.97``), ``H MM`` / ``H:MM`` clock-style cells (``8 00`` ->
8.0), bare integers, ``00:00:00`` zeros, comma decimals, and time-in/out pairs
where an "out" earlier than "in" implies a PM crossover (9:00-5:00 = 8h).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

_TIME = re.compile(r"^\s*(\d{1,2})[:.\s](\d{2})(?:[:.\s](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$",
                   re.IGNORECASE)


def parse_hours(value) -> Optional[float]:
    """Parse a cell that is meant to be a *quantity of hours*."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v >= 0 else None
    if isinstance(value, dt.time):
        # a time used as a duration: 8:00:00 -> 8.0
        return value.hour + value.minute / 60 + value.second / 3600
    s = str(value).strip().lower()
    if not s or s in {"-", "--", "n/a", "na", "off", "none"}:
        return None
    s = s.replace("hrs", "").replace("hours", "").replace("hour", "").replace("h", "").strip()
    s = s.replace("$", "").strip()

    # clock-style "8 00" / "8:00" / "121 00" -> H + MM/60.
    # NOTE: '.' is deliberately NOT a separator here -- "8.50" is decimal 8.5
    # hours, not 8h50m. Only ':' and whitespace mean clock notation.
    m = re.match(r"^(\d{1,3})[:\s](\d{2})$", s)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        if mm < 60:
            return h + mm / 60
    # plain decimal (allow comma decimal)
    s2 = s.replace(",", ".")
    try:
        v = float(s2)
        return v if v >= 0 else None
    except ValueError:
        return None


def parse_clock(value) -> Optional[dt.time]:
    """Parse a clock time like '7:30 AM', '15:30', '09:00:00'."""
    if value is None:
        return None
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.time()
    s = str(value).strip()
    if not s:
        return None
    m = _TIME.match(s)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    ss = int(m.group(3) or 0)
    ampm = (m.group(4) or "").replace(".", "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if h > 23 or mm > 59:
        return None
    return dt.time(h, mm, ss)


def hours_from_in_out(time_in, time_out, lunch_minutes: float = 0.0) -> Optional[float]:
    """Compute worked hours from in/out clock times.

    If 'out' is not after 'in', assume a 12h PM crossover (e.g. In 9:00, Out
    5:00 -> 17:00 -> 8h). Returns None if either side is unparseable or the pair
    is a 0/0 (treated as a non-working day by the caller).
    """
    ti = parse_clock(time_in)
    to = parse_clock(time_out)
    if ti is None or to is None:
        return None
    start = ti.hour + ti.minute / 60 + ti.second / 3600
    end = to.hour + to.minute / 60 + to.second / 3600
    if start == 0 and end == 0:
        return 0.0
    # If 'out' isn't after 'in', roll forward: +12 handles the AM/PM-ambiguous
    # day shift (9:00->5:00 = 8h); a second +12 (i.e. +24) handles a true
    # overnight/graveyard shift that crosses midnight (22:00->06:00 = 8h).
    if end <= start:
        end += 12
    if end <= start:
        end += 12
    worked = end - start - max(lunch_minutes, 0) / 60
    if worked < 0 or worked > 24:
        return None
    return round(worked, 2)


def split_regular_overtime(total: Optional[float], regular: Optional[float],
                           overtime: Optional[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Fill in whichever of (regular, overtime, total) can be derived."""
    r, o, t = regular, overtime, total
    if t is None and r is not None:
        t = (r or 0) + (o or 0)
    if r is None and t is not None:
        r = t - (o or 0) if o is not None else t
    if o is None and t is not None and r is not None:
        o = round(t - r, 2)
        if abs(o) < 1e-6:
            o = 0.0
    return r, o, t
