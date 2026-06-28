"""Merge per-file ``NormResult``s into one ``EmployeeMonth`` per person.

Responsibilities:
  * group results by employee identity (multiple weekly files -> one month)
  * lay every day of the month on a calendar
  * resolve same-day data from multiple sources (duplicate vs conflict) by trust
  * prorate weekly-only totals into the month without double-counting daily data
  * roll up monthly + per-client/project breakdowns and emit audit issues
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Optional

from ..normalize.normalizer import DayEntry, NormResult
from ..schema import (ClientBreakdown, DayRecord, EmployeeMonth, Issue,
                      IssueCode, IssueSeverity, SourceRef, WeeklyTotal)
from ..settings import Settings, get_settings
from .calendar import build_calendar_days

# trust by extractor / method (higher wins conflicts)
_TRUST = {
    "daily_grid": 3.0, "weekly_totals": 3.0, "excel": 3.0, "csv": 3.0,
    "pdf_native": 2.8, "llm_text": 2.4, "weekday_matrix": 2.0,
    "llm_vision": 1.9, "pdf_scanned": 1.2, "image": 1.2, "docx_media": 1.2,
}


def _trust(entry: DayEntry, res: NormResult) -> float:
    ext = (entry.source.extractor if entry.source else None) or res.method.split(":")[0]
    return _TRUST.get(ext, 1.5)


def _ident_key(name: Optional[str], emp_id: Optional[str]) -> str:
    if emp_id:
        return f"id:{re.sub(r'[^a-z0-9]', '', emp_id.lower())}"
    n = re.sub(r"[^a-z0-9 ]", "", (name or "unknown").lower()).strip()
    n = re.sub(r"\s+", " ", n)
    return f"nm:{n}" if n else "nm:unknown"


class EmployeeRegistry:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()

    def build(self, results: list[NormResult], month: int, year: int
              ) -> list[EmployeeMonth]:
        groups: dict[str, list[NormResult]] = defaultdict(list)
        for r in results:
            groups[_ident_key(r.employee_name, r.employee_id)].append(r)
        employees: list[EmployeeMonth] = []
        for rs in groups.values():
            # A weak/shared name (e.g. a company-template title) can collide
            # several *different* people. Split a name-group into sub-groups
            # whose daily data does not conflict on overlapping dates.
            clusters = _split_conflicting(rs)
            for cluster in clusters:
                em = self._build_one(cluster, month, year)
                if len(clusters) > 1:
                    hint = _disambiguator(cluster)
                    if hint:
                        base = em.employee_name or "Unknown"
                        # drop hint tokens already in the base name so we get
                        # 'Justin-Thason — Akkodis', not '... — Justin Thason Akkodis'.
                        seen = {t.lower().strip(".,-") for t in re.split(r"[\s\-]+", base)}
                        extra = " ".join(w for w in hint.split()
                                         if w.lower().strip(".,-") not in seen)
                        em.employee_name = f"{base} — {extra}" if extra else base
                    em.issues.append(Issue(
                        code=IssueCode.CONFLICT, severity=IssueSeverity.WARNING,
                        message="multiple distinct people shared a name/template; "
                                "split into separate records by conflicting data"))
                employees.append(em)
        return employees

    # ------------------------------------------------------------------ #
    def _build_one(self, results: list[NormResult], month: int, year: int
                   ) -> EmployeeMonth:
        name = next((r.employee_name for r in results if r.employee_name), None)
        emp_id = next((r.employee_id for r in results if r.employee_id), None)
        clients = _uniq([r.client for r in results if r.client])
        projects = _uniq([r.project for r in results if r.project])

        em = EmployeeMonth(
            employee_name=name, employee_id=emp_id, month=month, year=year,
            clients=clients, projects=projects,
            source_files=_uniq([r.file for r in results]),
            extraction_methods=_uniq([r.method for r in results]),
        )
        days = build_calendar_days(month, year, self.s)
        by_date = {d.date: d for d in days}

        # ---- collect entries per date across all sources ----
        per_date: dict[dt.date, list[tuple[DayEntry, NormResult]]] = defaultdict(list)
        for r in results:
            for e in r.entries:
                if e.date and e.date in by_date:
                    per_date[e.date].append((e, r))

        for d, items in per_date.items():
            self._resolve_day(by_date[d], items)

        # ---- weekly-only totals ----
        weekly = self._dedupe_weekly(results, em)
        em.weekly_totals = weekly

        # ---- rollups ----
        self._rollup(em, days, weekly, per_date)

        # ---- missing-day flags (working days with no data) ----
        # Only meaningful when we actually extracted *some* data; a record that
        # is entirely NEEDS_LLM should not spam 20+ "missing" flags -- the
        # employee-level NEEDS_LLM flag already says the source wasn't read.
        has_any_data = any(d.has_data for d in days) or bool(weekly)
        if has_any_data:
            for d in days:
                if (not d.is_weekend and not d.is_holiday and not d.has_data
                        and not self._covered_by_weekly(d.date, weekly)):
                    d.issues.append(Issue(
                        code=IssueCode.MISSING, severity=IssueSeverity.INFO, date=d.date,
                        message="no hours reported for a working day"))

        # a source may state only a month TOTAL with no daily/weekly grid (e.g.
        # an approval email "Approved 24 hours"). Use it when we have nothing else.
        stated = next((r.stated_total for r in results if r.stated_total is not None), None)
        if stated is not None and not has_any_data:
            em.monthly_regular = round(float(stated), 2)
            em.monthly_total = round(float(stated), 2)
            # no daily grid -> estimate days worked from the total (8h/day) so the
            # record doesn't misleadingly read "0 days" against a real month total.
            em.days_worked = round(float(stated) / 8.0)
            em.issues.append(Issue(
                code=IssueCode.WEEK_ONLY, severity=IssueSeverity.INFO,
                message=(f"only a stated month total ({stated}h) was available; "
                         "no daily breakdown — days worked inferred from total")))
        elif stated is not None and abs(stated - em.monthly_total) > 0.5:
            em.issues.append(Issue(
                code=IssueCode.TOTAL_MISMATCH, severity=IssueSeverity.WARNING,
                message=(f"source states monthly total {stated} but computed "
                         f"{em.monthly_total} from daily/weekly data")))

        # under-read guard: a vision/scan source yielding very little is more
        # likely a partial read than a genuine part-time month -> flag for review.
        used_vision = any(("vision" in (r.method or "") or "llm" in (r.method or ""))
                          for r in results)
        if used_vision and em.monthly_total < 80 and em.days_worked < 10:
            em.issues.append(Issue(
                code=IssueCode.NEEDS_LLM, severity=IssueSeverity.WARNING,
                message=(f"only {em.monthly_total}h / {em.days_worked} day(s) read from a "
                         "scanned/vision source -- likely under-read, please verify")))

        em.days = days
        em.confidence = round(
            sum(r.confidence for r in results) / max(len(results), 1), 2)
        em.client_breakdown = self._client_breakdown(em, results)
        if any(r.needs_llm for r in results):
            em.issues.append(Issue(
                code=IssueCode.NEEDS_LLM, severity=IssueSeverity.WARNING,
                message="some source(s) needed LLM/vision and may be incomplete"))
        return em

    # ------------------------------------------------------------------ #
    def _resolve_day(self, rec: DayRecord, items: list[tuple[DayEntry, NormResult]]):
        readable = [(e, r) for (e, r) in items
                    if any(v is not None for v in (e.regular, e.overtime, e.total))]
        # gather all sources for audit; carry per-entry issues but drop redundant
        # "unreadable" flags once any source produced a value for this day.
        for e, _ in items:
            if e.source:
                rec.sources.append(e.source)
            for iss in e.issues:
                if readable and iss.code == IssueCode.UNCLEAR and "unreadable" in iss.message:
                    continue
                rec.issues.append(iss)
        if not readable:
            return
        # rank by trust
        readable.sort(key=lambda er: _trust(er[0], er[1]), reverse=True)
        chosen, chosen_res = readable[0]

        if len(readable) > 1:
            totals = [round(e.total, 2) if e.total is not None else None
                      for e, _ in readable]
            distinct = {t for t in totals if t is not None}
            if len(distinct) > 1:
                rec.issues.append(Issue(
                    code=IssueCode.CONFLICT, severity=IssueSeverity.ERROR, date=rec.date,
                    message=("conflicting totals across sources: " +
                             ", ".join(f"{(e.source.file if e.source else '?')}={t}"
                                       for (e, _), t in zip(readable, totals))),
                    sources=[e.source for e, _ in readable if e.source]))
            else:
                rec.issues.append(Issue(
                    code=IssueCode.DUPLICATE, severity=IssueSeverity.INFO, date=rec.date,
                    message=f"same day reported in {len(readable)} sources (matching)",
                    sources=[e.source for e, _ in readable if e.source]))

        rec.regular_hours = chosen.regular
        rec.overtime_hours = chosen.overtime
        rec.total_hours = chosen.total
        rec.project = chosen.project
        rec.client = chosen_res.client
        rec.note = chosen.note or rec.note
        rec.raw = chosen.raw
        if rec.total_hours is not None and rec.total_hours > self.s.max_hours_per_day:
            rec.issues.append(Issue(
                code=IssueCode.OUT_OF_RANGE, severity=IssueSeverity.WARNING, date=rec.date,
                message=f"{rec.total_hours}h exceeds {self.s.max_hours_per_day}h/day"))

    # ------------------------------------------------------------------ #
    def _dedupe_weekly(self, results: list[NormResult], em: EmployeeMonth
                       ) -> list[WeeklyTotal]:
        buckets: dict[tuple, list[WeeklyTotal]] = defaultdict(list)
        for r in results:
            for w in r.weekly_totals:
                buckets[(w.week_start, w.week_end)].append(w)
        out: list[WeeklyTotal] = []
        for key, ws in buckets.items():
            if len(ws) == 1:
                out.append(ws[0])
                continue
            totals = [w.total_hours for w in ws if w.total_hours is not None]
            distinct = {round(t, 2) for t in totals}
            keep = max(ws, key=lambda w: (w.total_hours or 0))
            srcs = [s for w in ws for s in w.sources]
            if len(distinct) > 1:
                em.issues.append(Issue(
                    code=IssueCode.CONFLICT, severity=IssueSeverity.ERROR,
                    message=(f"conflicting weekly totals for {key[0]}..{key[1]}: "
                             f"{sorted(distinct)} (kept {keep.total_hours})"),
                    sources=srcs))
            else:
                em.issues.append(Issue(
                    code=IssueCode.DUPLICATE, severity=IssueSeverity.INFO,
                    message=f"duplicate weekly total for {key[0]}..{key[1]}",
                    sources=srcs))
            keep.sources = srcs
            out.append(keep)
        return sorted(out, key=lambda w: w.week_start)

    def _covered_by_weekly(self, d: dt.date, weekly: list[WeeklyTotal]) -> bool:
        return any(w.week_start <= d <= w.week_end for w in weekly)

    # ------------------------------------------------------------------ #
    def _rollup(self, em: EmployeeMonth, days: list[DayRecord],
                weekly: list[WeeklyTotal],
                per_date: dict[dt.date, list]):
        reg = sum(d.regular_hours for d in days if d.regular_hours is not None)
        ot = sum(d.overtime_hours for d in days if d.overtime_hours is not None)
        tot = sum(d.total_hours for d in days if d.total_hours is not None)
        worked = sum(1 for d in days if (d.total_hours or 0) > 0)

        # Each calendar day must contribute exactly ONCE. Daily data is
        # authoritative for its dates; a weekly total then contributes only for
        # the in-month days it covers that are NOT already counted (by daily data
        # or by an earlier weekly total). This prevents double-counting when the
        # same period is reported by more than one source (e.g. a CSV + an image).
        covered: set[dt.date] = {d.date for d in days if d.has_data}
        used_weekly = 0
        for w in sorted(weekly, key=lambda w: w.week_start):
            span = (w.week_end - w.week_start).days + 1
            in_month = [w.week_start + dt.timedelta(days=k) for k in range(span)
                        if (w.week_start + dt.timedelta(days=k)).month == em.month
                        and (w.week_start + dt.timedelta(days=k)).year == em.year]
            uncovered = [d for d in in_month if d not in covered]
            if not uncovered:
                continue   # this week's days are already counted -> skip (no double count)
            frac = len(uncovered) / max(span, 1)
            wt = w.total_hours
            if wt is not None:
                tot += wt * frac
            if w.regular_hours is not None:
                reg += w.regular_hours * frac
            elif wt is not None:               # weekly total not split -> treat as regular
                reg += wt * frac
            if w.overtime_hours is not None:
                ot += w.overtime_hours * frac
            if wt is not None:                 # estimate worked days for weekly-only sources
                worked += round(wt * frac / 8.0)
            covered.update(uncovered)
            used_weekly += 1
        if weekly:
            em.issues.append(Issue(
                code=IssueCode.WEEK_ONLY, severity=IssueSeverity.INFO,
                message=(f"{len(weekly)} week(s) provided as weekly totals only; "
                         "monthly figure prorated to the target month")))

        em.monthly_regular = round(reg, 2)
        em.monthly_overtime = round(ot, 2)
        em.monthly_total = round(tot, 2)
        em.days_worked = worked

    # ------------------------------------------------------------------ #
    def _client_breakdown(self, em: EmployeeMonth, results: list[NormResult]
                          ) -> list[ClientBreakdown]:
        agg: dict[tuple, ClientBreakdown] = {}
        default_client = em.clients[0] if em.clients else None
        for d in em.days:
            if not d.has_data:
                continue
            key = (d.client or default_client, d.project)
            cb = agg.get(key)
            if cb is None:
                cb = ClientBreakdown(client=key[0], project=key[1])
                agg[key] = cb
            cb.regular_hours += d.regular_hours or 0
            cb.overtime_hours += d.overtime_hours or 0
            cb.total_hours += d.total_hours or 0
            if (d.total_hours or 0) > 0:
                cb.days_worked += 1
        for cb in agg.values():
            cb.regular_hours = round(cb.regular_hours, 2)
            cb.overtime_hours = round(cb.overtime_hours, 2)
            cb.total_hours = round(cb.total_hours, 2)
        return list(agg.values())


def _uniq(items: list[Optional[str]]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _day_map(r: NormResult) -> dict:
    return {e.date: e.total for e in r.entries if e.total is not None}


def _conflicts(a: dict, b: dict) -> bool:
    """Two results conflict if they report different totals on >=2 shared days."""
    shared = set(a) & set(b)
    diff = sum(1 for d in shared if abs((a[d] or 0) - (b[d] or 0)) > 0.5)
    return diff >= 2


def _split_conflicting(results: list[NormResult]) -> list[list[NormResult]]:
    """Greedily cluster results so that members of a cluster never conflict on
    overlapping dates. Disjoint weeks for the same person stay together; the same
    dates with different values (different people) split apart."""
    clusters: list[dict] = []
    for r in results:
        rmap = _day_map(r)
        placed = False
        for cl in clusters:
            if not _conflicts(cl["dates"], rmap):
                for d, t in rmap.items():
                    cl["dates"].setdefault(d, t)
                cl["members"].append(r)
                placed = True
                break
        if not placed:
            clusters.append({"dates": dict(rmap), "members": [r]})
    return [cl["members"] for cl in clusters]


def _disambiguator(cluster: list[NormResult]) -> Optional[str]:
    """A short hint (from the source filename's trailing words) to tell split
    same-named records apart, e.g. 'Jane Doe' from '... Jane Doe.pdf'."""
    import os
    from ..ingest.excel import _COMPANY_STOPWORDS, _NAME_STOPWORDS
    stem = os.path.splitext(os.path.basename(cluster[0].file))[0]
    s = re.sub(r"[_\-]+", " ", stem)
    s = re.sub(r"\b20\d{2}\b|\d{1,2}[/.]\d{1,2}([/.]\d{2,4})?|\d{5,}", " ", s)
    s = re.sub(r"(?i)\b(timesheets?|time\s*sheet|ts|april|apr|bi\s*monthly|"
               r"jan|feb|mar|may|jun|jul|aug|sep|oct|nov|dec)\b", " ", s)
    # keep only plausible name tokens: not company names, not all-caps labels,
    # not generic stopwords (avoids 'AJACE — AJACE' / '— 15' style corruption).
    words = [w for w in s.split()
             if len(w) > 2 and not w.isupper()
             and w.lower() not in _COMPANY_STOPWORDS
             and w.lower() not in _NAME_STOPWORDS]
    return " ".join(words[-3:]).strip() or None
