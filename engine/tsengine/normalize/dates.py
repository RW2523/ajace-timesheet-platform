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
from dataclasses import dataclass, field
from typing import Iterable, Optional

_NUMERIC_DATE = re.compile(r"\b(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})\b")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})

# Default work week: Mon-Fri (0=Mon .. 6=Sun). The engine's real weekend set is
# configurable (Settings.weekend_set); callers pass it in so this stays generic.
_DEFAULT_WEEKEND: frozenset[int] = frozenset({5, 6})


def find_date_tokens(text: str) -> list[str]:
    return [m.group(0) for m in _NUMERIC_DATE.finditer(text)]


# numeric + common textual date shapes; used to harvest in-document dates cheaply
# (only real date-looking substrings are parsed, so we never feed junk to the
# slow dateutil fallback).
_ANY_DATE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{1,2}[ \-/][A-Za-z]{3,9}[ \-/,]+\d{2,4}"
    r"|[A-Za-z]{3,9}[ \-/.]+\d{1,2},?\s+\d{2,4}")


def collect_dates(text: str, order: str = "MDY",
                  default_year: Optional[int] = None) -> list[dt.date]:
    """All parseable dates in a blob of text (numeric + textual forms)."""
    out: list[dt.date] = []
    for m in _ANY_DATE.finditer(text or ""):
        d = parse_date(m.group(0), order, default_year)
        if d is not None:
            out.append(d)
    return out


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


def month_bounds(month: int, year: int) -> tuple[dt.date, dt.date]:
    """First and last calendar day of the target month."""
    return (dt.date(year, month, 1),
            dt.date(year, month, calendar.monthrange(year, month)[1]))


def span_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every calendar date in [start, end] inclusive (order-normalized)."""
    if end < start:
        start, end = end, start
    return [start + dt.timedelta(days=k) for k in range((end - start).days + 1)]


@dataclass
class WeeklyClip:
    """How a lump weekly total should be split into the target month.

    A weekly total is a single number for a whole week; when the week straddles
    a month boundary we must attribute only the in-month portion. The correct
    unit is the WORKDAY, not the calendar day: a 40h week spanning Sun..Sat that
    reaches into the month by one Friday contributes 8h (one workday), not
    40*days/7. ``fraction`` is (in-month contributing days / basis days) so the
    caller can prorate total/regular/overtime consistently.
    """
    fraction: float               # portion of the lump that lands in-month & uncounted
    basis_days: list[dt.date] = field(default_factory=list)      # days the lump spreads over
    contributing_days: list[dt.date] = field(default_factory=list)  # in-month, not-yet-covered
    worked_days: int = 0          # contributing WORKDAYS (for days_worked estimation)
    weekend_basis: bool = False   # True if we fell back to spreading over all 7 days


def clip_weekly_to_month(week_start: dt.date, week_end: dt.date, month: int,
                         year: int, *, hours: Optional[float] = None,
                         weekend: Iterable[int] = _DEFAULT_WEEKEND,
                         max_per_day: float = 16.0,
                         covered: Optional[set[dt.date]] = None) -> WeeklyClip:
    """Clip a lump weekly total to its in-month, not-yet-counted portion.

    Generic across all months and week alignments:
      * The lump is distributed over the week's WORKDAYS (``weekend`` excluded),
        which matches the near-universal Mon-Fri pattern and fixes the classic
        ``total * calendar_days / 7`` straddle over-count.
      * If the implied per-workday rate would exceed ``max_per_day`` the week
        must include weekend work, so we fall back to spreading over all days
        (never under-count a genuine 7-day week).
      * ``covered`` (dates already counted by daily entries or an earlier week)
        are excluded so a day is never counted twice.
    """
    weekend_set = set(weekend) if weekend is not None else set(_DEFAULT_WEEKEND)
    covered = covered or set()
    days = span_days(week_start, week_end)
    workdays = [d for d in days if d.weekday() not in weekend_set]

    # choose the distribution basis: workdays, unless the hours can't fit in them
    basis = workdays
    weekend_basis = False
    if hours is not None and workdays and (hours / len(workdays)) > max_per_day:
        basis, weekend_basis = days, True
    elif not workdays:                       # a week with no workdays (all weekend)
        basis, weekend_basis = days, True

    contributing = [d for d in basis
                    if d.month == month and d.year == year and d not in covered]
    frac = (len(contributing) / len(basis)) if basis else 0.0
    worked = sum(1 for d in contributing if d.weekday() not in weekend_set)
    return WeeklyClip(fraction=round(frac, 6), basis_days=basis,
                      contributing_days=contributing, worked_days=worked,
                      weekend_basis=weekend_basis)


# --------------------------------------------------------------------------- #
# Period resolution: agree on the (month, year) a file really covers, from up to
# three independent signals -- the requested/folder period, the filename, and the
# in-sheet date histogram. A 2-of-3 majority wins; a dissent is surfaced, and a
# genuine 3-way disagreement is flagged as a conflict rather than silently clipped.
# --------------------------------------------------------------------------- #
# whole-word month tokens only (full names + abbreviations), longest first so
# 'march' wins over 'mar'. \b on both sides stops false hits like 'may' inside
# 'Mayank' or 'apr' inside a random token.
_MON_ALT = "|".join(sorted(
    {m.lower() for m in calendar.month_name if m}
    | {m.lower() for m in calendar.month_abbr if m},
    key=len, reverse=True))
_FILENAME_MONTH = re.compile(rf"\b({_MON_ALT})\b", re.IGNORECASE)


def month_from_filename(name: str, default_year: Optional[int] = None
                        ) -> Optional[tuple[int, int]]:
    """Best (month, year) guess from a filename. Prefers a spelled month token
    ('TS May -2026'); falls back to a compact ISO or numeric date
    ('20260529', '4.30.26'). Returns None when no month signal is present."""
    if not name:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(name))   # drop extension
    base = default_year or 2000

    # (a) spelled month name, then the nearest 4-digit (or adjacent 2-digit) year
    m = _FILENAME_MONTH.search(stem)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            y4 = re.search(r"\b(20\d{2})\b", stem)
            if y4:
                return (mon, int(y4.group(1)))
            y2 = re.search(r"[’'\-_ ](\d{2})\b", stem[m.end():])
            if y2:
                return (mon, _norm_year(int(y2.group(1)), base))
            return (mon, default_year or base)

    # (b) compact ISO 'YYYYMMDD' (e.g. Timesheet-20260529)
    iso = re.search(r"(20\d{2})(\d{2})(\d{2})", stem)
    if iso:
        yy, mm, dd = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return (mm, yy)

    # (c) numeric date token 'M.D.YY' / 'M-D-YYYY' etc. -- month is the first
    # component when it is a plausible month, else the second.
    nd = _NUMERIC_DATE.search(stem)
    if nd:
        a, b, c = int(nd.group(1)), int(nd.group(2)), int(nd.group(3))
        if a > 31 and 1 <= b <= 12:            # YYYY-MM-DD
            return (b, a)
        mon = a if 1 <= a <= 12 else (b if 1 <= b <= 12 else None)
        if mon:
            year = _norm_year(c, base) if c > 0 else (default_year or base)
            if year < 100:
                year = _norm_year(year, base)
            return (mon, year)
    return None


def month_histogram(dates: Iterable[Optional[dt.date]]) -> dict[tuple[int, int], int]:
    """Count of parsed dates per (month, year)."""
    hist: dict[tuple[int, int], int] = {}
    for d in dates:
        if d is None or d.year < 1990:
            continue
        hist[(d.month, d.year)] = hist.get((d.month, d.year), 0) + 1
    return hist


def dominant_month(dates: Iterable[Optional[dt.date]], *, min_share: float = 0.5,
                   min_count: int = 2) -> Optional[tuple[int, int]]:
    """The (month, year) that a clear plurality of in-sheet dates fall in.

    Requires at least ``min_count`` supporting dates AND a ``min_share`` majority
    so a couple of stray boundary dates never flip the vote.
    """
    hist = month_histogram(dates)
    if not hist:
        return None
    total = sum(hist.values())
    ranked = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)
    (best, n) = ranked[0]
    # reject ties (a 50/50 split has no dominant month)
    if len(ranked) > 1 and ranked[1][1] == n:
        return None
    if n >= min_count and (n / total) >= min_share:
        return best
    return None


@dataclass
class PeriodResolution:
    month: int
    year: int
    status: str                       # 'confirmed' | 'remapped' | 'conflict'
    requested: tuple[int, int]
    filename: Optional[tuple[int, int]] = None
    sheet: Optional[tuple[int, int]] = None
    note: Optional[str] = None

    @property
    def mismatch(self) -> bool:
        return self.status != "confirmed"


def _fmt(my: Optional[tuple[int, int]]) -> str:
    return f"{my[1]}-{my[0]:02d}" if my else "?"


def resolve_target_period(requested_month: int, requested_year: int, *,
                          filename: Optional[str] = None,
                          sheet_dates: Optional[Iterable[Optional[dt.date]]] = None
                          ) -> PeriodResolution:
    """Reconcile the requested period against filename + in-sheet evidence.

    Three signals vote: the requested/folder period (always present), the
    filename month, and the dominant in-sheet month. Rules (generic, any month):
      * filename AND sheet agree on a period different from requested -> that is
        strong evidence the file was filed under the wrong month: REMAPPED (the
        caller decides whether to act on it), always with a mismatch note.
      * any two signals agree -> CONFIRMED on the agreed period (normally the
        requested one; a lone dissenter is noted but doesn't move the period).
      * no two agree (or the only extra signal contradicts) -> CONFLICT: keep the
        requested period but flag it for review.
    """
    req = (requested_month, requested_year)
    fn = month_from_filename(filename or "", default_year=requested_year)
    sh = dominant_month(list(sheet_dates)) if sheet_dates is not None else None

    def res(month_year, status, note=None):
        return PeriodResolution(month=month_year[0], year=month_year[1],
                                status=status, requested=req,
                                filename=fn, sheet=sh, note=note)

    # strong signal: filename and content agree on a DIFFERENT month
    if fn and sh and fn == sh and fn != req:
        return res(fn, "remapped",
                   f"filename and sheet dates both indicate {_fmt(fn)}, "
                   f"not the requested {_fmt(req)}")

    # majority vote among the available signals
    votes: dict[tuple[int, int], int] = {req: 1}
    for v in (fn, sh):
        if v is not None:
            votes[v] = votes.get(v, 0) + 1

    top, n = max(votes.items(), key=lambda kv: kv[1])
    if n >= 2:
        dissent = [v for v in (fn, sh) if v is not None and v != top]
        note = None
        if dissent:
            src = "filename" if fn in dissent else "sheet dates"
            note = f"{src} suggest {_fmt(dissent[0])} but {_fmt(top)} confirmed by majority"
        return res(top, "confirmed", note)

    # only the requested period is known (no filename/content signal) -> nothing
    # contradicts it; trust the request.
    others = [v for v in (fn, sh) if v is not None]
    if not others:
        return res(req, "confirmed")

    # a lone signal disagrees with the request (no majority) -> flag for review
    parts = [f"requested {_fmt(req)}"]
    if fn:
        parts.append(f"filename {_fmt(fn)}")
    if sh:
        parts.append(f"sheet dates {_fmt(sh)}")
    return res(req, "conflict", "period signals disagree: " + ", ".join(parts))
