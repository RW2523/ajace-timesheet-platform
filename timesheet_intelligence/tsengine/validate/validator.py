"""Final consistency validation over assembled ``EmployeeMonth`` records.

Registry already handled duplicates/conflicts/missing/weekly proration. This
layer adds per-day field consistency (regular+overtime vs total), invalid
values, and optional LLM-assisted reconciliation of remaining hard conflicts.
"""
from __future__ import annotations

from typing import Optional

from ..llm.prompts import reconcile_messages
from ..llm.router import ModelRouter
from ..schema import (DayRecord, EmployeeMonth, Issue, IssueCode, IssueSeverity)
from ..settings import Settings, get_settings


class Validator:
    def __init__(self, settings: Optional[Settings] = None,
                 router: Optional[ModelRouter] = None):
        self.s = settings or get_settings()
        self.router = router

    def validate(self, em: EmployeeMonth) -> EmployeeMonth:
        for d in em.days:
            self._check_day(d)
        self._maybe_reconcile(em)
        return em

    def _check_day(self, d: DayRecord):
        r, o, t = d.regular_hours, d.overtime_hours, d.total_hours
        for label, v in (("regular", r), ("overtime", o), ("total", t)):
            if v is not None and v < 0:
                d.issues.append(Issue(
                    code=IssueCode.INVALID, severity=IssueSeverity.ERROR, date=d.date,
                    message=f"negative {label} hours ({v})"))
        if r is not None and o is not None and t is not None:
            if abs((r + o) - t) > 0.05:
                d.issues.append(Issue(
                    code=IssueCode.TOTAL_MISMATCH, severity=IssueSeverity.WARNING, date=d.date,
                    message=f"regular {r} + overtime {o} != total {t}"))
        # data on a weekend/holiday is worth surfacing (not an error)
        if (d.is_weekend or d.is_holiday) and (d.total_hours or 0) > 0:
            d.issues.append(Issue(
                code=IssueCode.OUT_OF_RANGE, severity=IssueSeverity.INFO, date=d.date,
                message=("hours reported on a " +
                         ("holiday" if d.is_holiday else "weekend"))))

    def _maybe_reconcile(self, em: EmployeeMonth):
        """If hard conflicts remain and the LLM is enabled, ask it to adjudicate.
        Conservative: it only annotates; it never silently overwrites."""
        if not (self.router and self.router.enabled):
            return
        conflicts = [d for d in em.days
                     if any(i.code == IssueCode.CONFLICT for i in d.issues)]
        if not conflicts:
            return
        blob_lines = []
        for d in conflicts:
            srcs = "; ".join(s.label() for s in d.sources)
            blob_lines.append(f"{d.date}: total={d.total_hours} sources=[{srcs}]")
        msgs = reconcile_messages(em.employee_name or "unknown",
                                  "\n".join(blob_lines), em.month, em.year)
        out = self.router.run("validate", msgs)
        if out.ok and isinstance(out.data, dict):
            notes = out.data.get("notes") or []
            for n in notes:
                em.issues.append(Issue(
                    code=IssueCode.CONFLICT, severity=IssueSeverity.INFO,
                    message=f"LLM reconciliation note: {n}"))
