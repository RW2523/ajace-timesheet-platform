"""Top-level pipeline: folder + month/year -> ProcessingReport.

Flow per file:
    detect -> extract (orchestrator) -> deterministic normalize
           -> [escalate to LLM/vision if needed & enabled]
    ... then group by employee (registry) -> validate -> report.

The pipeline is robust: a single unreadable file is recorded as ``unprocessed``
and never aborts the run. With no API key the engine still produces a full
report from the deterministic + local-OCR paths, flagging what an LLM could
improve.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from pathlib import Path
from typing import Optional

from .aggregate.registry import EmployeeRegistry
from .llm.router import ModelRouter
from .normalize.llm_normalizer import LLMNormalizer
from .normalize.normalizer import NormResult, Normalizer
from .orchestrator import Orchestrator
from .schema import (ENGINE_VERSION, AgentAction, ExtractionQuality, FileTrace,
                     ProcessingReport, UnprocessedFile)
from .settings import Settings, get_settings
from .validate.validator import Validator

log = logging.getLogger("tsengine.pipeline")

SKIP_NAMES = {"desktop.ini", ".ds_store", "thumbs.db"}
SKIP_PREFIX = ("~$", ".")
_MONTH_TOKENS = {m.lower() for m in
                 ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                  "sep", "oct", "nov", "dec", "january", "february", "march",
                  "april", "june", "july", "august", "september", "october",
                  "november", "december"]}


def _client_from_path(rel_path: str) -> Optional[str]:
    parts = Path(rel_path).parts
    if len(parts) < 2:
        return None
    raw = parts[0]
    # strip timesheet/month/year noise
    cleaned = re.sub(r"[_\-]+", " ", raw)
    cleaned = re.sub(r"\b(ts|timesheet|timesheets|employees?)\b", " ", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"\b20\d{2}\b", " ", cleaned)
    tokens = [t for t in cleaned.split() if t.lower() not in _MONTH_TOKENS]
    name = " ".join(tokens).strip()
    return name or None


_INVOICE_NUM_RE = re.compile(r"invoice\s*#|invoice\s*(no|number)\b", re.I)
_FILENAME_INV_RE = re.compile(r"[-_ ]inv[-_ ]?\d", re.I)


def _looks_like_invoice(text: str, filename: str = "") -> bool:
    """Heuristic: is this a billing/invoice document rather than a timesheet?

    Invoices in these folders carry hours+names too, so without this they create
    phantom employees. Conservative: needs clear invoice structure (or an invoice
    filename plus a weak signal) so real timesheets are never skipped.
    """
    t = (text or "").lower()
    fn = (filename or "").lower()
    if not t.strip() and not fn:
        return False
    score = 0
    if _INVOICE_NUM_RE.search(t):
        score += 2
    if "bill to" in t:
        score += 2
    if any(k in t for k in ("amount due", "total due", "balance due")):
        score += 2
    if "rate" in t and "amount" in t and "$" in (text or ""):
        score += 1
    has_invoice_word = "invoice" in t
    fn_inv = bool(_FILENAME_INV_RE.search(fn))
    if has_invoice_word and score >= 2:
        return True
    if fn_inv and (has_invoice_word or score >= 1):
        return True
    return False


class TimesheetPipeline:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()
        self.orch = Orchestrator(self.s)
        self.normalizer = Normalizer(self.s)
        self.router = ModelRouter(self.s)
        self.llm_norm = LLMNormalizer(self.router, self.s)
        self.registry = EmployeeRegistry(self.s)
        self.validator = Validator(self.s, self.router)

    # ------------------------------------------------------------------ #
    def discover(self, folder: str | Path) -> list[Path]:
        root = Path(folder)
        files: list[Path] = []
        for p in sorted(root.rglob("*")):
            if p.is_symlink():        # don't follow symlinks out of the folder
                continue
            if not p.is_file():
                continue
            if p.name.lower() in SKIP_NAMES:
                continue
            if p.name.startswith(SKIP_PREFIX):
                continue
            files.append(p)
        return files

    # ------------------------------------------------------------------ #
    def process_folder(self, folder: str | Path, month: int, year: int
                       ) -> ProcessingReport:
        root = Path(folder)
        report = ProcessingReport(
            folder=str(root), month=month, year=year,
            generated_at=dt.datetime.now().isoformat(timespec="seconds"),
            engine_version=ENGINE_VERSION,
            flow=self.s.flow,
        )
        all_results: list[NormResult] = []
        files = self.discover(root)
        report.files_seen = len(files)

        for path in files:
            rel = str(path.relative_to(root))
            client_hint = _client_from_path(rel)
            try:
                results = self._process_one(path, rel, month, year, client_hint, report)
            except Exception as exc:
                log.exception("file failed: %s", path)
                report.unprocessed.append(UnprocessedFile(
                    file=rel, reason=f"pipeline error: {exc}"))
                continue
            if results is None:
                continue
            all_results.extend(results)
            report.files_processed += 1

        report.llm_used = self.router.calls > 0
        usage = self.router.usage_summary()
        report.llm_calls = usage["calls"]
        report.llm_tokens = usage["total_tokens"]
        report.llm_cost_usd = usage["total_cost_usd"]
        report.llm_usage_by_model = usage["by_model"]
        employees = self.registry.build(all_results, month, year)
        for em in employees:
            self.validator.validate(em)
        # newest/most-complete first
        employees.sort(key=lambda e: (-(e.monthly_total or 0),
                                      e.employee_name or "zzz"))
        report.employees = employees
        return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def _agent_for(method: str) -> str:
        """Which internal sub-agent a NormResult's method string belongs to."""
        m = method or ""
        if "vision" in m:
            return "VisionReader"
        if ":local/" in m or "local/" in m:
            return "Normalizer:Local"
        if m.startswith("llm"):
            return "Normalizer:Cloud"
        return "Normalizer"

    def _process_one(self, path: Path, rel: str, month: int, year: int,
                     client_hint: Optional[str], report: ProcessingReport
                     ) -> Optional[list[NormResult]]:
        trace = FileTrace(file=rel, flow=self.s.flow) if self.s.agent_trace else None

        def act(agent: str, action: str, detail: str = "",
                model: Optional[str] = None, ok: bool = True) -> None:
            if trace is not None:
                trace.actions.append(AgentAction(
                    agent=agent, action=action, detail=detail[:300],
                    model=model, ok=ok))

        def done(handled_by: str) -> None:
            if trace is not None:
                trace.handled_by = handled_by
                report.agent_traces.append(trace)

        det = self.orch.detect(path)
        act("Classifier", "classified", f"{det.kind.value} ({det.reason})")
        raw = self.orch.extract(path, det)
        # restore folder-relative file label for nicer audit trails
        raw.file = rel
        act("Parser", "extracted",
            f"{len(raw.tables)} table(s), {len(raw.text)} chars, {len(raw.images)} image(s)")
        if raw.meta.get("ocr_confidence") is not None:
            act("OCR", "ocr",
                f"{raw.meta.get('ocr_engine', 'tesseract')} conf "
                f"{raw.meta.get('ocr_confidence')}")

        if raw.quality == ExtractionQuality.EMPTY and not raw.text and not raw.tables \
                and not raw.images:
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="; ".join(raw.notes) or "nothing extractable"))
            act("Parser", "rejected", "nothing extractable", ok=False)
            done("Parser")
            return None

        # Skip invoices / billing docs -- they carry names+hours and would
        # otherwise be mistaken for timesheets (phantom employees).
        if _looks_like_invoice(raw.text, rel):
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="appears to be an invoice / billing document, not a timesheet"))
            act("Classifier", "rejected", "invoice/billing document, not a timesheet")
            done("Classifier")
            return None

        results = self.normalizer.normalize(raw, month, year, client_hint)
        for res in results:
            act("Normalizer", "normalized",
                f"{res.method}, conf {res.confidence:.2f}, {len(res.entries)} entries"
                + (", needs_llm" if res.needs_llm else ""))

        # decide on LLM escalation
        for i, res in enumerate(results):
            if self._should_escalate(res):
                improved = self._escalate(raw, month, year, client_hint, res, act)
                if improved is not None:
                    results[i] = improved

        # selective SECOND OPINION: re-run only HARD results on the stronger
        # model. Budget flow hard-disables this (second_opinion_model == "").
        if self.s.second_opinion_model and self.llm_norm.enabled:
            for i, res in enumerate(results):
                if self._needs_second_opinion(res):
                    better = self._second_opinion(raw, month, year, client_hint, res)
                    if better is not None:
                        if better is not res:
                            w, t = self._worked_total(better)
                            act("Reconciler", "second_opinion",
                                f"kept {better.method}: {t}h/{w}d",
                                model=self.s.second_opinion_model)
                        results[i] = better
        done(self._agent_for(results[0].method) if results else "Normalizer")
        return results

    def _should_escalate(self, res: NormResult) -> bool:
        if not self.llm_norm.enabled:
            return False
        if self.s.llm_policy == "always":
            return True
        return res.needs_llm or res.confidence < self.s.llm_confidence_threshold

    def _escalate(self, raw, month, year, client_hint, current: NormResult,
                  act=None) -> Optional[NormResult]:
        act = act or (lambda *a, **k: None)
        try:
            llm_res = self.llm_norm.normalize(raw, month, year, client_hint)
        except Exception as exc:
            log.warning("LLM escalation failed for %s: %s", raw.file, exc)
            act("Normalizer:Cloud", "escalated", f"failed: {exc}", ok=False)
            return None
        if llm_res is None:
            return None

        # Plausibility gate on FREE local-model reads (bench: qwen can fabricate a
        # full month of 0h entries on sparse sheets). Implausible -> retry on cloud.
        if ":local/" in (llm_res.method or "") or "local/" in (llm_res.method or ""):
            worked, total = self._worked_total(llm_res)
            has_any = llm_res.entries or llm_res.weekly_totals or llm_res.stated_total
            if not has_any or worked > 23 or total > 300:
                act("Normalizer:Local", "rejected",
                    f"implausible local read ({total}h/{worked}d) -- retrying on cloud",
                    model=llm_res.method.split(":", 1)[-1], ok=False)
                try:
                    cloud = self.llm_norm.normalize(raw, month, year, client_hint,
                                                    no_local=True)
                except Exception:
                    cloud = None
                if cloud is not None:
                    llm_res = cloud
            else:
                act("Normalizer:Local", "normalized",
                    f"{total}h/{worked}d (free local model)",
                    model=llm_res.method.split(":", 1)[-1])

        # Score by *usable* data (entries/weeks carrying at least one hour value),
        # not raw cardinality -- a vision pass can emit one all-null entry per day
        # which must NOT displace a smaller-but-accurate deterministic result.
        def useful(res):
            e = sum(1 for x in res.entries
                    if any(v is not None for v in (x.regular, x.overtime, x.total)))
            w = sum(1 for x in res.weekly_totals
                    if any(v is not None for v in (x.regular_hours, x.overtime_hours,
                                                   x.total_hours)))
            return e + w

        cur_data, llm_data = useful(current), useful(llm_res)
        # replace only on a strict improvement (or when deterministic had nothing)
        if llm_data > cur_data or (cur_data == 0 and llm_data > 0):
            llm_res.notes.append(f"escalated from deterministic ({current.method})")
            act(self._agent_for(llm_res.method), "accepted",
                f"{llm_res.method}: {llm_data} usable entries (was {cur_data})",
                model=(llm_res.method.split(":", 1)[-1] if ":" in llm_res.method else None))
            return llm_res
        act(self._agent_for(llm_res.method), "rejected",
            f"no improvement over {current.method} ({llm_data} vs {cur_data} usable)",
            ok=False)
        return current

    @staticmethod
    def _worked_total(res: NormResult) -> tuple[int, float]:
        worked = sum(1 for e in res.entries if (e.total or 0) > 0)
        total = sum((e.total or 0) for e in res.entries)
        return worked, round(total, 2)

    def _needs_second_opinion(self, res: NormResult) -> bool:
        """Is this result uncertain enough to spend the stronger model on?"""
        if res.needs_llm or res.confidence < self.s.escalation_min_confidence:
            return True
        # implausible LLM read -> likely a mis-count (over/under-read). More worked
        # days than a month has weekdays, or a wildly high/low monthly total.
        if "llm" in (res.method or ""):
            worked, total = self._worked_total(res)
            if worked > 23 or total > 240 or (res.entries and total < 40):
                return True
        return False

    def _second_opinion(self, raw, month, year, client_hint, current: NormResult
                        ) -> Optional[NormResult]:
        """Re-run extraction on the stronger escalation model (gemini). It's the
        more trusted reader on hard files, so keep its result whenever it produces
        a PLAUSIBLE read -- this corrects both over- and under-reads from the cheap
        primary model, not just sparse ones."""
        model = self.s.second_opinion_model
        keys = [f"TSE_MODEL_{t.upper()}" for t in
                ("classify", "vision", "table", "normalize", "validate")]
        old = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ[k] = model
        try:
            alt = self.llm_norm.normalize(raw, month, year, client_hint)
        except Exception as exc:
            log.warning("second-opinion (%s) failed for %s: %s", model, raw.file, exc)
            return None
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        if alt is None:
            return None
        worked, total = self._worked_total(alt)
        plausible = (worked > 0 or alt.weekly_totals or alt.stated_total) \
            and worked <= 24 and total <= 300
        if plausible:
            alt.notes.append(f"second opinion via {model} (replaced {current.method})")
            return alt
        return current


def process_folder(folder: str | Path, month: int, year: int,
                   settings: Optional[Settings] = None) -> ProcessingReport:
    return TimesheetPipeline(settings or get_settings()).process_folder(folder, month, year)
