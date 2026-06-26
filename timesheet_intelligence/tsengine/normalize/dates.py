"""Date parsing & month filtering.

The hard problem: ``01/04/2026`` is April 1 in a day-first (DMY) source and
January 4 in a month-first (MDY) source. We never guess globally -- we *infer
the order per file* from evidence (a component > 12 is decisive) and, failing
that, choose the order that places the most dates inside the requested month.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from typing import Iterable, Optional

_NUMERIC_DATE = re.compile(r"\b(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})\b")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})


def find_date_tokens(text: str) -> list[str]:
    return [m.group(0) for m in _NUMERIC_DATE.finditer(text)]


def infer_date_order(samples: Iterable[str], month: int, year: int) -> str:
    """Return 'MDY' or 'DMY' for purely-numeric dates in this file."""
    first_gt12 = second_gt12 = 0
    toks: list[tuple[int, int, int]] = []
    for s in samples:
        for m in _NUMERIC_DATE.finditer(s):
            a, b, c = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            # skip ISO yyyy-mm-dd (a is the year)
            if a > 31:
                continue
            toks.append((a, b, c))
            if a > 12:
                first_gt12 += 1
            if b > 12:
                second_gt12 += 1
    if first_gt12 and not second_gt12:
        return "DMY"
    if second_gt12 and not first_gt12:
        return "MDY"
    # ambiguous: pick the order that lands the most tokens in the target month
    def hits(order: str) -> int:
        n = 0
        for a, b, _ in toks:
            mm = a if order == "MDY" else b
            if mm == month:
                n += 1
        return n
    return "MDY" if hits("MDY") >= hits("DMY") else "DMY"


def _norm_year(y: int, default_year: int) -> int:
    if y >= 100:
        return y
    # anchor a 2-digit year to the century closest to the document's year
    base = default_year or 2000
    candidates = (1900 + y, 2000 + y, 2100 + y)
    return min(candidates, key=lambda c: abs(c - base))


def rescue_epoch_date(d: Optional[dt.date], month: int, year: int) -> Optional[dt.date]:
    """Rescue Excel-1900-epoch-corrupted dates.

    A very common spreadsheet failure stores a day-of-month as a serial number,
    so a real April-2026 date surfaces as e.g. 1900-01-20. When the year is
    implausibly old, we keep the day-of-month and re-anchor it to the target
    month/year if that yields a valid date. Returns None if it cannot be rescued.
    """
    if d is None or d.year >= 1990:
        return None
    ndays = calendar.monthrange(year, month)[1]
    if 1 <= d.day <= ndays:
        return dt.date(year, month, d.day)
    return None


def parse_date(s, order: str = "MDY", default_year: Optional[int] = None) -> Optional[dt.date]:
    """Parse a single date-ish value. Handles ISO, textual months, and numeric
    dates using the supplied component order."""
    if s is None:
        return None
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    text = str(s).strip()
    if not text:
        return None

    # ISO yyyy-mm-dd
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # textual month: 1-Apr-26, 30-Mar-2026, Apr 1 2026, April 1, 2026
    tm = re.search(r"(\d{1,2})[ \-/]([A-Za-z]{3,9})[ \-/,]+(\d{2,4})", text)
    if tm and tm.group(2).lower() in _MONTHS:
        d, mon, y = int(tm.group(1)), _MONTHS[tm.group(2).lower()], _norm_year(int(tm.group(3)), default_year or 2000)
        try:
            return dt.date(y, mon, d)
        except ValueError:
            pass
    tm2 = re.search(r"([A-Za-z]{3,9})[ \-/.]+(\d{1,2})[ ,]+(\d{2,4})", text)
    if tm2 and tm2.group(1).lower() in _MONTHS:
        mon, d, y = _MONTHS[tm2.group(1).lower()], int(tm2.group(2)), _norm_year(int(tm2.group(3)), default_year or 2000)
        try:
            return dt.date(y, mon, d)
        except ValueError:
            pass

    # numeric N/N/N
    nm = _NUMERIC_DATE.search(text)
    if nm:
        a, b, c = int(nm.group(1)), int(nm.group(2)), int(nm.group(3))
        if a > 31:  # yyyy/mm/dd form caught loosely
            try:
                return dt.date(a, b, c)
            except ValueError:
                return None
        if order == "DMY":
            day, mon = a, b
        else:
            mon, day = a, b
        year = _norm_year(c, default_year or 2000)
        try:
            return dt.date(year, mon, day)
        except ValueError:
            # tolerate swapped values (e.g. 13/04 parsed as MDY)
            try:
                return dt.date(year, day, mon)
            except ValueError:
                return None

    # last resort: dateutil for odd textual forms
    try:
        from dateutil import parser as dparser

        return dparser.parse(text, dayfirst=(order == "DMY"),
                             default=dt.datetime(default_year or 2000, 1, 1)).date()
    except Exception:
        return None


def in_target_month(d: Optional[dt.date], month: int, year: int) -> bool:
    return bool(d) and d.month == month and d.year == year


def month_days(month: int, year: int) -> list[dt.date]:
    n = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, day) for day in range(1, n + 1)]


def week_overlap_fraction(start: dt.date, end: dt.date, month: int, year: int) -> float:
    """Fraction of [start,end] inclusive that falls within the target month."""
    if end < start:
        start, end = end, start
    m_start = dt.date(year, month, 1)
    m_end = dt.date(year, month, calendar.monthrange(year, month)[1])
    lo = max(start, m_start)
    hi = min(end, m_end)
    if hi < lo:
        return 0.0
    total = (end - start).days + 1
    inside = (hi - lo).days + 1
    return inside / total
