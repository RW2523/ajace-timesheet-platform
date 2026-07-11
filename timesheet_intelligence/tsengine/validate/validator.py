"""Final consistency validation over assembled ``EmployeeMonth`` records.

Registry already handled duplicates/conflicts/missing/weekly proration. This
layer adds per-day field consistency (regular+overtime vs total), invalid
values, and optional LLM-assisted reconciliation of remaining hard conflicts.
"""
from __future__ import annotations

from typing import Optional

import calendar as _calendar
import datetime as _dt

from ..llm.prompts import reconcile_messages
from ..llm.router import ModelRouter
from ..schema import (DayRecord, EmployeeMonth, Issue, IssueCode, IssueSeverity,
                      ReviewStatus, VerificationStatus)
from ..settings import Settings, get_settings


class Validator:
    def __init__(self, settings: Optional[Settings] = None,
                 router: Optional[ModelRouter] = None):
        self.s = settings or get_settings()
        self.router = router

    def validate(self, em: EmployeeMonth) -> EmployeeMonth:
        for d in em.days:
            self._check_day(d)
        self._check_month(em)
        self._maybe_reconcile(em)
        em.review_status = self._route(em)
        return em

    # -- month-level sanity checks (the "after extraction" gaps) --------------
    def _check_month(self, em: EmployeeMonth):
        # 1) implausibly high month total (over-read) -- upper bound complements
        #    the registry's under-read guard.
        if (em.monthly_total or 0) > 230:
            em.issues.append(Issue(
                code=IssueCode.OUT_OF_RANGE, severity=IssueSeverity.WARNING,
                message=f"month total {em.monthly_total}h is unusually high (>230h); verify"))
        # 2) days_worked cannot exceed the working days in the month.
        weekdays = sum(1 for day in range(1, _calendar.monthrange(em.year, em.month)[1] + 1)
                       if _dt.date(em.year, em.month, day).weekday() < 5)
        if (em.days_worked or 0) > weekdays:
            em.issues.append(Issue(
                code=IssueCode.INVALID, severity=IssueSeverity.ERROR,
                message=(f"{em.days_worked} days worked exceeds {weekdays} weekdays "
                         f"in the month -- impossible; re-check")))
        # 3) overtime present -> flag for explicit approval sign-off.
        if (em.monthly_overtime or 0) > 0:
            em.issues.append(Issue(
                code=IssueCode.OUT_OF_RANGE, severity=IssueSeverity.INFO,
                message=f"{em.monthly_overtime}h overtime present; confirm it was approved"))

    # -- human-review routing gate -------------------------------------------
    def _route(self, em: EmployeeMonth) -> str:
        issues = em.all_issues
        has_error = any(i.severity == IssueSeverity.ERROR for i in issues)
        has_conflict = any(i.code in (IssueCode.CONFLICT, IssueCode.NEEDS_LLM)
                           for i in issues)
        has_warning = any(i.severity == IssueSeverity.WARNING for i in issues)
        conf = em.confidence or 0.0
        clean = not has_error and not has_warning \
            and conf >= self.s.direct_autoaccept_confidence

        # verification: a flow may have set it explicitly (the consensus two-key
        # gate, an email vote). For flows that don't, DERIVE it -- a clean,
        # confident read is treated as confirmed so legacy behaviour is preserved.
        vs = em.verification_status
        if vs in (None, VerificationStatus.UNVERIFIED.value) \
                and em.flow != "consensus":
            vs = (VerificationStatus.CONFIRMED.value if clean
                  else VerificationStatus.UNVERIFIED.value)
        em.verification_status = vs or VerificationStatus.UNVERIFIED.value

        if has_error or has_conflict or conf < 0.6:
            return ReviewStatus.BLOCKED.value
        # THE structural rule: nothing auto-accepts unless it is CONFIRMED.
        if em.verification_status != VerificationStatus.CONFIRMED.value:
            return ReviewStatus.NEEDS_REVIEW.value
        if not clean:
            return ReviewStatus.NEEDS_REVIEW.value
        return ReviewStatus.AUTO_ACCEPTED.value

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
