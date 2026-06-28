"""Deterministic normalizer: RawExtraction -> NormResult(s).

Strategies, tried per table and scored:
  * daily_grid     -- a row per calendar date with hour/in-out columns
  * weekly_totals  -- week-ending or start/end ranges with summed hours
  * weekday_matrix -- Sun..Sat columns (project matrices); needs a date anchor

Whatever no strategy can confidently read is returned with low confidence and a
NEEDS_LLM note so the pipeline can escalate to the OpenRouter LLM/vision layer.
The normalizer never invents numbers; unreadable cells become issues.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from ..schema import (ExtractionQuality, Issue, IssueCode, IssueSeverity,
                      RawExtraction, RawTable, SourceRef, WeeklyTotal)
from ..settings import Settings, get_settings
from . import dates as D
from . import hours as H

# --------------------------------------------------------------------------- #
ROLE_KEYWORDS: dict[str, list[str]] = {
    "date": ["date"],
    "regular": ["regular", "reg hrs", "reg.", "straight", "st/fp", "st hours",
                "regula", "billable hours", "worked hours"],
    "overtime": ["overtime", "ot hours", "o.t", "overti", "ot"],
    "total": ["total"],
    "in": ["time in", "clock in", "start time", "start", "in"],
    "out": ["time out", "clock out", "finish time", "finish", "out"],
    "lunch_out": ["lunch out", "break out"],
    "lunch_in": ["lunch in", "break in"],
    "hours": ["hours", "hrs", "# of hours"],
    "week_start": ["week start", "start date", "timesheet start", "period start",
                   "week beginning"],
    "week_end": ["week end", "week ending", "end date", "timesheet end", "period end"],
    "project": ["project", "client", "customer", "task", "program"],
    "note": ["note", "comment", "description", "shift notes"],
    "sick": ["sick"],
    "vacation": ["vacation", "pto"],
    "holiday": ["holiday"],
    "leave": ["leave"],
}
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Explicit precedence so SPECIFIC roles claim a column before the generic
# 'hours' fallback. Critical: headers like "Number of Regular Hours" and
# "Number of Overtime Hours" both contain "hours"; without this ordering the
# generic 'hours' role would swallow the overtime column and OT would be lost.
ROLE_PRIORITY = [
    "date", "week_start", "week_end",
    "regular", "overtime", "total",
    "sick", "vacation", "holiday", "leave",
    "lunch_out", "lunch_in", "in", "out",
    "project", "note",
    "hours",  # last-resort generic
]


@dataclass
class DayEntry:
    date: dt.date
    regular: Optional[float] = None
    overtime: Optional[float] = None
    total: Optional[float] = None
    project: Optional[str] = None
    note: Optional[str] = None
    raw: Optional[str] = None
    source: Optional[SourceRef] = None
    issues: list[Issue] = field(default_factory=list)


@dataclass
class NormResult:
    file: str
    method: str
    quality: ExtractionQuality
    employee_name: Optional[str] = None
    employee_id: Optional[str] = None
    client: Optional[str] = None
    project: Optional[str] = None
    entries: list[DayEntry] = field(default_factory=list)
    weekly_totals: list[WeeklyTotal] = field(default_factory=list)
    stated_total: Optional[float] = None
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    needs_llm: bool = False


def _norm(s) -> str:
    return ("" if s is None else str(s)).strip().lower()


def _kw_match(cell: str, kw: str) -> bool:
    """Substring match for descriptive headers; word-boundary match for short
    tokens like 'in'/'out'/'ot' so they don't fire inside unrelated words."""
    if len(kw) <= 3:
        return cell == kw or cell.startswith(kw + " ") or cell.endswith(" " + kw)
    return kw in cell


def _assign_roles(header_row: list) -> dict[str, int]:
    """Map a role -> column index for a header row (first match wins per column).

    Roles are resolved in ROLE_PRIORITY order (specific before generic) so the
    'hours' fallback never steals a Regular/Overtime/Total column.
    """
    roles: dict[str, int] = {}
    norm_cells = [_norm(c) for c in header_row]
    for role in ROLE_PRIORITY:
        kws = ROLE_KEYWORDS.get(role)
        if not kws or role in roles:
            continue
        for ci, cell in enumerate(norm_cells):
            if ci in roles.values():
                continue
            if any(_kw_match(cell, kw) for kw in kws):
                roles[role] = ci
                break
    return roles


def _find_header_row(grid: list[list], max_scan: int = 15) -> tuple[int, dict[str, int]]:
    """Pick the row that yields the most useful column roles."""
    best_idx, best_roles, best_score = -1, {}, 0
    for i, row in enumerate(grid[:max_scan]):
        roles = _assign_roles(row)
        score = len(roles) + (2 if "date" in roles else 0) + \
            (2 if {"week_start", "week_end"} & set(roles) else 0)
        if score > best_score:
            best_idx, best_roles, best_score = i, roles, score
    return best_idx, best_roles


# --------------------------------------------------------------------------- #
def _strategy_daily_grid(table: RawTable, order: str, month: int, year: int,
                         max_per_day: float) -> Optional[NormResult]:
    grid = [r for r in table.rows]
    if len(grid) < 2:
        return None
    hdr_idx, roles = _find_header_row(grid)
    if "date" not in roles:
        return None
    date_col = roles["date"]

    res = NormResult(file=table.source.file, method="daily_grid",
                     quality=ExtractionQuality.NATIVE)
    parsed = candidates = cross_month = 0
    for ri in range(hdr_idx + 1, len(grid)):
        row = grid[ri]
        if date_col >= len(row):
            continue
        cell = row[date_col]
        # capture a stated total row for cross-check
        rowtext = " ".join(_norm(c) for c in row)
        d = D.parse_date(cell, order, year)
        if d is None:
            # capture an explicitly-labeled MONTH total for cross-checking
            if "month" in rowtext and "total" in rowtext:
                for ci in range(len(row)):
                    st = H.parse_hours(row[ci])
                    if st is not None and st > 0:
                        res.stated_total = st
                        break
            continue
        recovered = False
        if d.year < 1990:
            rescued = D.rescue_epoch_date(d, month, year)
            if rescued is not None:
                d = rescued
                recovered = True
        if not D.in_target_month(d, month, year):
            cross_month += 1
            continue

        def col(role):
            ci = roles.get(role)
            return row[ci] if ci is not None and ci < len(row) else None

        regular = H.parse_hours(col("regular"))
        overtime = H.parse_hours(col("overtime"))
        total = H.parse_hours(col("total"))
        hours_generic = H.parse_hours(col("hours"))
        if total is None and hours_generic is not None and regular is None:
            total = hours_generic

        # time-in/out fallback
        if regular is None and total is None:
            ti, to = col("in"), col("out")
            if ti is not None or to is not None:
                lunch = 0.0
                lo, li = col("lunch_out"), col("lunch_in")
                lo_t, li_t = H.parse_clock(lo), H.parse_clock(li)
                if lo_t and li_t:
                    lunch = ((li_t.hour * 60 + li_t.minute) -
                             (lo_t.hour * 60 + lo_t.minute))
                    lunch = lunch if lunch > 0 else 0.0
                computed = H.hours_from_in_out(ti, to, lunch)
                if computed is not None:
                    total = computed

        regular, overtime, total = H.split_regular_overtime(total, regular, overtime)
        has_val = any(v is not None for v in (regular, overtime, total))
        # was there any hour-bearing content in this row at all?
        raw_present = any(_clean(col(r)) is not None
                          for r in ("regular", "overtime", "total", "hours", "in", "out"))
        if not has_val and not raw_present:
            # genuinely blank day (e.g. weekend left empty) -> not a failure,
            # the calendar will render it; don't flag or penalize confidence.
            continue

        candidates += 1
        entry = DayEntry(
            date=d, regular=regular, overtime=overtime, total=total,
            note=_clean(col("note")), project=_clean(col("project")),
            raw=" | ".join("" if c is None else str(c) for c in row)[:300],
            source=SourceRef(file=table.source.file, sheet=table.source.sheet,
                             page=table.source.page, row=ri + 1,
                             column=str(date_col), extractor="daily_grid"),
        )
        if total is not None and total > max_per_day:
            entry.issues.append(Issue(
                code=IssueCode.OUT_OF_RANGE, severity=IssueSeverity.WARNING,
                date=d, message=f"{total}h exceeds {max_per_day}h/day bound",
                sources=[entry.source]))
        if recovered:
            entry.issues.append(Issue(
                code=IssueCode.UNCLEAR, severity=IssueSeverity.INFO, date=d,
                message=(f"date cell was corrupted (year {cell}); recovered "
                         f"day-of-month into {year}-{month:02d}"),
                sources=[entry.source]))
        if has_val:
            parsed += 1
        else:
            # content was present but unreadable -> genuinely unclear
            entry.issues.append(Issue(
                code=IssueCode.UNCLEAR, severity=IssueSeverity.WARNING, date=d,
                message="date present but hours unreadable", sources=[entry.source]))
        res.entries.append(entry)

    if not res.entries:
        return None
    if cross_month:
        res.notes.append(f"{cross_month} row(s) outside {year}-{month:02d} ignored")
    res.confidence = round(parsed / max(candidates, 1), 2) if candidates else 0.0
    return res


def _strategy_weekly_totals(table: RawTable, order: str, month: int, year: int
                            ) -> Optional[NormResult]:
    grid = [r for r in table.rows]
    if not grid:
        return None
    # header row could be the provided headers (CSV) or inside the grid (Excel)
    if table.headers:
        hdr = table.headers
        roles = _assign_roles(hdr)
        data_rows = grid
        hdr_in_grid = False
    else:
        hdr_idx, roles = _find_header_row(grid)
        hdr = grid[hdr_idx] if hdr_idx >= 0 else []
        data_rows = grid[hdr_idx + 1:]
        hdr_in_grid = True
    has_range = "week_end" in roles or "week_start" in roles
    if not has_range:
        return None
    if "total" not in roles and "regular" not in roles and "hours" not in roles:
        return None

    res = NormResult(file=table.source.file, method="weekly_totals",
                     quality=ExtractionQuality.NATIVE)
    count = 0
    for ri, row in enumerate(data_rows):
        def col(role):
            ci = roles.get(role)
            return row[ci] if ci is not None and ci < len(row) else None

        ws = D.parse_date(col("week_start"), order, year)
        we = D.parse_date(col("week_end"), order, year)
        if ws is None and we is None:
            rowtext = " ".join(_norm(c) for c in row)
            if "month" in rowtext and "total" in rowtext:
                for c in row:
                    st = H.parse_hours(c)
                    if st is not None and st > 0:
                        res.stated_total = st
                        break
            continue
        if ws is None:
            ws = we - dt.timedelta(days=6)
        if we is None:
            we = ws + dt.timedelta(days=6)
        frac = D.week_overlap_fraction(ws, we, month, year)
        if frac <= 0:
            continue
        total = H.parse_hours(col("total"))
        regular = H.parse_hours(col("regular"))
        if regular is None:                       # 0.0 is a real value, keep it
            regular = H.parse_hours(col("hours"))
        overtime = H.parse_hours(col("overtime"))
        if total is None and regular is None and overtime is None:
            continue
        regular, overtime, total = H.split_regular_overtime(total, regular, overtime)
        src = SourceRef(file=table.source.file, sheet=table.source.sheet,
                        page=table.source.page,
                        row=ri + 1, extractor="weekly_totals")
        res.weekly_totals.append(WeeklyTotal(
            week_start=ws, week_end=we, regular_hours=regular,
            overtime_hours=overtime, total_hours=total,
            in_month_fraction=round(frac, 3), sources=[src],
            note=_clean(col("note"))))
        count += 1
    if not res.weekly_totals:
        return None
    res.confidence = 0.7 if count else 0.0
    res.notes.append("only weekly totals available (no daily breakdown)")
    return res


def _strategy_weekday_matrix(table: RawTable, order: str, month: int, year: int,
                             context_text: str) -> Optional[NormResult]:
    """Sun..Sat column matrices (e.g. project exports). Needs a date anchor for
    the columns; if none is found, returns a low-confidence NEEDS_LLM marker."""
    header = table.headers or (table.rows[0] if table.rows else [])
    norm_hdr = [_norm(c) for c in header]
    day_cols = {i: dn for i, c in enumerate(norm_hdr)
                for dn in DAY_NAMES if c == dn or c.startswith(dn[:3]) and len(c) <= 4}
    if len(day_cols) < 5:
        return None
    # find a period anchor "... 03/29/2026 to 04/04/2026 ..."
    import re
    anchor = None
    m = re.search(r"(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}).{0,6}(?:to|-|–|—).{0,6}"
                  r"(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4})", context_text)
    if m:
        anchor = D.parse_date(m.group(1), order, year)
    res = NormResult(file=table.source.file, method="weekday_matrix",
                     quality=ExtractionQuality.NATIVE)
    if anchor is None:
        res.needs_llm = True
        res.confidence = 0.2
        res.notes.append("weekday matrix detected but no date anchor; needs LLM")
        return res
    # map columns (sorted) to consecutive dates from the anchor
    sorted_cols = sorted(day_cols.keys())
    col_dates = {ci: anchor + dt.timedelta(days=k) for k, ci in enumerate(sorted_cols)}
    per_day: dict[dt.date, float] = {}
    data_rows = table.rows if table.headers else table.rows[1:]
    for row in data_rows:
        for ci, d in col_dates.items():
            if not D.in_target_month(d, month, year):
                continue
            if ci < len(row):
                hv = H.parse_hours(row[ci])
                if hv:
                    per_day[d] = per_day.get(d, 0.0) + hv
    for d, tot in sorted(per_day.items()):
        res.entries.append(DayEntry(
            date=d, total=round(tot, 2),
            source=SourceRef(file=table.source.file, page=table.source.page,
                             extractor="weekday_matrix"),
            raw=f"sum across project rows = {round(tot, 2)}"))
    res.confidence = 0.6 if res.entries else 0.2
    if not res.entries:
        res.needs_llm = True
    return res


_DATE_LINE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"
    r"|\b\d{1,2}[ \-/][A-Za-z]{3,9}[ \-/,]+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}[ \-/.]+\d{1,2},?\s+\d{2,4}\b")
_HOURS_LABEL = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:hours|hrs|hour)\b", re.IGNORECASE)
# a bare decimal hours value (the dot keeps it distinct from clock times like 7:00)
_HOURS_VALUE = re.compile(r"(?<![:\d.])(\d{1,2}\.\d{1,2})(?![:\d])")
# an integer hours value immediately followed by a pay-code word
_HOURS_PAYCODE = re.compile(
    r"(?<![:\d.])(\d{1,2})\s+(?:regular|over\s*time|overtime|o\.?t\.?|holiday|"
    r"vacation|sick|worked|billable|straight)\b", re.IGNORECASE)


def _strategy_text_hours_labeled(text: str, file: str, order: str, month: int,
                                 year: int, max_per_day: float) -> Optional[NormResult]:
    """Safe, high-precision text strategy for timecards that label hours
    explicitly (e.g. a timecard export: '30-Mar-2026' then '8.00 Hours'). The
    'Hours' keyword is what makes this unambiguous; we never grab a bare number.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    n = len(lines)
    res = NormResult(file=file, method="text_hours_labeled",
                     quality=ExtractionQuality.NATIVE)
    # collect every labeled-hours value seen for each date
    per_date: dict[dt.date, list[float]] = {}
    raws: dict[dt.date, str] = {}
    for i, line in enumerate(lines):
        low = line.lower()
        # skip date-range / period header lines (ambiguous anchors)
        if "period" in low or len(_DATE_LINE.findall(line)) > 1:
            continue
        m = _DATE_LINE.search(line)
        if not m:
            continue
        d = D.parse_date(m.group(0), order, year)
        if not D.in_target_month(d, month, year):
            continue
        # (a) same-line "<date> <hours> <paycode>" rows (common HR/timesheet
        #     exports, e.g. "4/1/2026  8.00  Regular Time"). Take a decimal
        #     value, or an integer immediately followed by a pay-code word.
        after = line[m.end():]
        sm = _HOURS_VALUE.search(after) or _HOURS_PAYCODE.search(after)
        if sm:
            val = float(sm.group(1))
            if 0 <= val <= max_per_day:
                per_date.setdefault(d, []).append(val)
                raws.setdefault(d, line[:200])
                continue
        # (b) labeled "<n> Hours" on this or a following line (timecard detail)
        for j in range(i, min(i + 4, n)):
            hm = _HOURS_LABEL.search(lines[j])
            if hm:
                val = float(hm.group(1))
                if 0 <= val <= max_per_day:
                    per_date.setdefault(d, []).append(val)
                    raws.setdefault(d, line[:200])
                break
    ambiguous = 0
    for d, vals in sorted(per_date.items()):
        distinct = sorted({round(v, 2) for v in vals})
        if len(distinct) == 1:
            # repeated identical readings (detail + summary views) -> one value.
            # Derive the regular/overtime split AT THE SOURCE -- if left None the
            # registry rollup drops them and monthly_regular wrongly reads 0.
            r, o, t = H.split_regular_overtime(distinct[0], None, None)
            res.entries.append(DayEntry(
                date=d, regular=r, overtime=o, total=t,
                source=SourceRef(file=file, extractor="text_hours_labeled"),
                raw=raws.get(d)))
        else:
            # genuinely conflicting labeled values -> never guess; flag it
            ambiguous += 1
            src = SourceRef(file=file, extractor="text_hours_labeled")
            res.entries.append(DayEntry(
                date=d, total=None, source=src, raw=raws.get(d),
                issues=[Issue(code=IssueCode.UNCLEAR, severity=IssueSeverity.WARNING,
                              date=d, sources=[src],
                              message=f"multiple labeled hours for this day: {distinct}")]))
    if not res.entries:
        return None
    res.confidence = 0.72 if not ambiguous else 0.45
    if ambiguous:
        res.needs_llm = True
    res.notes.append("parsed explicitly-labeled '<n> Hours' timecard entries")
    return res


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]


def _strategy_summary_total(tables, month: int, year: int) -> "Optional[NormResult]":
    """Pull a stated monthly total from a one-row summary table.

    Handles approval emails / cover sheets like:
        Name | Emp Id | Project | Period | Total Hours
        ...  |  ...    |  ...    | May 2026 | 160
    Runs only as a fallback (after grid strategies fail), so it never overrides a
    real daily breakdown. Emits a stated_total -> the registry uses it as the
    month total when there's no daily/weekly data.
    """
    mname = _MONTH_NAMES[month - 1]
    tokens = {mname, mname[:3], f"{month}/", f"-{month}-", f"{year}"}
    for table in tables:
        rows = [["" if c is None else str(c).strip() for c in r]
                for r in ([table.headers] + list(table.rows))]
        for hi, hrow in enumerate(rows):
            total_col = period_col = None
            for ci, cell in enumerate(hrow):
                cl = cell.lower()
                if cl in ("total", "total hours", "total hrs", "totalhours") or \
                        ("total" in cl and ("hour" in cl or "hrs" in cl)):
                    total_col = ci
                if cl in ("period", "month", "month/year", "duration"):
                    period_col = ci
            if total_col is None:
                continue
            for drow in rows[hi + 1:]:
                if total_col >= len(drow):
                    continue
                val = H.parse_hours(drow[total_col])
                if val is None or val <= 0:
                    continue
                # if a period column exists, require it to match the target month
                if period_col is not None and period_col < len(drow):
                    per = drow[period_col].lower()
                    if per and not any(tok in per for tok in tokens):
                        continue
                res = NormResult(file=table.source.file, method="summary_total",
                                 quality=ExtractionQuality.NATIVE, confidence=0.6)
                res.stated_total = float(val)
                res.notes.append(f"month total {val:g}h read from a summary table")
                return res
    return None


# --------------------------------------------------------------------------- #
class Normalizer:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()

    def normalize(self, raw: RawExtraction, month: int, year: int,
                  client_hint: Optional[str] = None) -> list[NormResult]:
        order = D.infer_date_order(
            [raw.text] + [str(c) for t in raw.tables for r in t.rows for c in r],
            month, year)
        results: list[NormResult] = []
        for table in raw.tables:
            best = self._best_for_table(table, order, month, year, raw.text)
            if best:
                results.append(best)

        merged = self._merge_table_results(results, raw) if results else None
        if merged and (merged.entries or merged.weekly_totals) and not merged.needs_llm:
            self._attach_identity(merged, raw, client_hint)
            return [merged]

        # Safe text fallback for explicitly-labeled "<n> Hours" timecards.
        if raw.text:
            try:
                text_res = _strategy_text_hours_labeled(
                    raw.text, raw.file, order, month, year, self.s.max_hours_per_day)
            except Exception:
                text_res = None
            if text_res and text_res.entries:
                if merged:
                    text_res.weekly_totals.extend(merged.weekly_totals)
                    text_res.notes.extend(merged.notes)
                self._attach_identity(text_res, raw, client_hint)
                return [text_res]

        # Summary/cover-sheet table that only states a monthly TOTAL (e.g. an
        # approval email "Total Hours: 160" for the period) -- use it directly
        # rather than burning a vision call that can't find a daily grid.
        summ = _strategy_summary_total(raw.tables, month, year)
        if summ is not None:
            self._attach_identity(summ, raw, client_hint)
            return [summ]

        # Nothing structured worked -> mark for LLM/vision escalation.
        fallback = NormResult(
            file=raw.file, method="deterministic_insufficient",
            quality=raw.quality, needs_llm=True, confidence=0.1)
        if merged:
            fallback.entries = merged.entries
            fallback.weekly_totals = merged.weekly_totals
            fallback.confidence = max(0.1, merged.confidence * 0.5)
        fallback.notes.append(
            "deterministic extraction insufficient; LLM/vision recommended")
        self._attach_identity(fallback, raw, client_hint)
        return [fallback]

    def _best_for_table(self, table, order, month, year, context_text):
        candidates = []
        for fn in (_strategy_daily_grid,):
            try:
                r = fn(table, order, month, year, self.s.max_hours_per_day)
                if r:
                    candidates.append(r)
            except Exception:
                pass
        for fn in (_strategy_weekly_totals,):
            try:
                r = fn(table, order, month, year)
                if r:
                    candidates.append(r)
            except Exception:
                pass
        try:
            r = _strategy_weekday_matrix(table, order, month, year, context_text)
            if r:
                candidates.append(r)
        except Exception:
            pass
        if not candidates:
            return None
        # prefer the strategy with the most real data, then confidence
        candidates.sort(key=lambda r: (len(r.entries) + len(r.weekly_totals),
                                       r.confidence), reverse=True)
        return candidates[0]

    def _merge_table_results(self, results: list[NormResult], raw: RawExtraction
                             ) -> NormResult:
        merged = NormResult(file=raw.file, method="+".join(
            sorted({r.method for r in results})), quality=raw.quality)
        for r in results:
            merged.entries.extend(r.entries)
            merged.weekly_totals.extend(r.weekly_totals)
            merged.notes.extend(r.notes)
            if r.stated_total is not None:
                merged.stated_total = r.stated_total
            merged.needs_llm = merged.needs_llm or r.needs_llm
        # confidence = data-weighted average
        if results:
            merged.confidence = round(
                sum(r.confidence for r in results) / len(results), 2)
        merged.needs_llm = merged.needs_llm and not (merged.entries or merged.weekly_totals)
        return merged

    def _attach_identity(self, res: NormResult, raw: RawExtraction,
                         client_hint: Optional[str]):
        res.employee_name = res.employee_name or raw.meta.get("name_hint")
        if not res.client:
            res.client = client_hint
        res.notes.extend(raw.notes)
