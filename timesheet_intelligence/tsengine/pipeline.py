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
from .normalize import dates as D
from .normalize.llm_normalizer import LLMNormalizer
from .normalize.normalizer import (NormResult, Normalizer,
                                   _has_month_scale_evidence)
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
        self._direct = None   # lazily created DirectExtractor (flow="direct")

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
            em.flow = self.s.flow
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
        if m.startswith("direct"):
            return "DirectReader"
        if "vision" in m:
            return "VisionReader"
        if ":local/" in m or "local/" in m:
            return "Normalizer:Local"
        if m.startswith("llm"):
            return "Normalizer:Cloud"
        return "Normalizer"

    def _process_direct(self, path, rel, month, year, client_hint, det,
                        report, act, done) -> "Optional[list[NormResult]]":
        """flow='direct': one exhaustive model request per file, no parsing."""
        from .direct.extractor import DirectExtractor
        from .ingest.excel import _name_hint

        if self._direct is None:
            self._direct = DirectExtractor(self.router, self.s)
        name_hint = None
        try:
            name_hint = _name_hint(Path(rel).stem)
        except Exception:
            pass
        results = self._direct.extract(path, rel, month, year, client_hint,
                                       name_hint, det.kind, act)
        if not results:
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="direct extraction: not a timesheet or unreadable"))
            done("DirectReader")
            return None
        # period sanity: filename + the dates the model actually returned
        try:
            entry_dates = [e.date for r in results for e in r.entries if e.date]
            pr = D.resolve_target_period(month, year, filename=Path(rel).name,
                                         sheet_dates=entry_dates)
            if pr.mismatch:
                for r in results:
                    r.period_mismatch = pr.note
                act("PeriodResolver", "period_mismatch", pr.note or pr.status, ok=False)
        except Exception as exc:
            log.debug("period resolver (direct) failed for %s: %s", rel, exc)
        done(self._agent_for(results[0].method))
        return results

    # ------------------------------------------------------------------ #
    def _process_consensus(self, path, rel, month, year, client_hint, det,
                           report, act, done) -> "Optional[list[NormResult]]":
        """flow='consensus': require two independent agreeing derivations.

        Key A = the hardened deterministic read (steps 1-3). Key B = a blind
        whole-file model read. A file whose deterministic sum equals its own
        printed total exits at $0 (no model call). Otherwise Key B runs and the
        consensus gate + four locks decide; disagreement routes to review.
        """
        raw = self.orch.extract(path, det)
        raw.file = rel
        act("Parser", "extracted",
            f"{len(raw.tables)} table(s), {len(raw.text)} chars, {len(raw.images)} image(s)")
        if raw.quality == ExtractionQuality.EMPTY and not raw.text and not raw.tables \
                and not raw.images:
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="; ".join(raw.notes) or "nothing extractable"))
            act("Parser", "rejected", "nothing extractable", ok=False)
            done("Parser")
            return None
        if _looks_like_invoice(raw.text, rel):
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="appears to be an invoice / billing document, not a timesheet"))
            act("Classifier", "rejected", "invoice/billing document, not a timesheet")
            done("Classifier")
            return None

        # KEY A: hardened deterministic read
        a_results = self.normalizer.normalize(raw, month, year, client_hint)
        self._resolve_period(rel, raw, a_results, month, year, act)
        key_a = a_results[0] if a_results else None
        if key_a is not None:
            aw, at = self._worked_total(key_a)
            act("Consensus", "key_a",
                f"{key_a.method}: {at:g}h/{aw}d conf {key_a.confidence:.2f}")

        # S2: $0 verified exit -- deterministic sum == the sheet's own printed total
        if key_a is not None and self._printed_total_confirms(key_a, month, year):
            key_a.confidence = max(key_a.confidence, 0.9)
            key_a.needs_llm = False
            key_a.verification = "confirmed"       # printed total = second key
            key_a.notes.append(
                f"consensus $0 exit: deterministic sum matches the sheet's printed "
                f"total ({key_a.stated_total:g}h)")
            act("Consensus", "confirmed",
                f"printed-total match ({key_a.stated_total:g}h); no model call")
            done(self._agent_for(key_a.method))
            return a_results

        # KEY B: blind whole-file model read (independent of Key A)
        key_b = None
        if self.llm_norm.enabled:
            if self._direct is None:
                from .direct.extractor import DirectExtractor
                self._direct = DirectExtractor(self.router, self.s)
            from .ingest.excel import _name_hint
            try:
                nh = _name_hint(Path(rel).stem)
            except Exception:
                nh = None
            try:
                b_results = self._direct.extract(
                    path, rel, month, year, client_hint, nh, det.kind, act,
                    ladder=self.s.consensus_key_b_ladder)
            except Exception as exc:
                log.warning("consensus key B failed for %s: %s", rel, exc)
                b_results = None
            key_b = b_results[0] if b_results else None
            if key_b is not None:
                bw, bt = self._worked_total(key_b)
                act("Consensus", "key_b",
                    f"{key_b.method}: {bt:g}h/{bw}d conf {key_b.confidence:.2f}")

        chosen = self._consensus_gate(key_a, key_b, month, year, act)
        if chosen is None:
            report.unprocessed.append(UnprocessedFile(
                file=rel, file_type=det.kind.value,
                reason="consensus: no usable deterministic or model read"))
            done("Consensus")
            return None
        if key_a is not None and key_a.period_mismatch and not chosen.period_mismatch:
            chosen.period_mismatch = key_a.period_mismatch
        done(self._agent_for(chosen.method))
        return [chosen]

    def _printed_total_confirms(self, res: NormResult, month: int, year: int) -> bool:
        """The document's own arithmetic is a genuine second derivation: a
        vote-valid DAILY read whose in-month sum equals the sheet's printed total,
        landing in a sane month. Lets a clean file auto-accept at $0."""
        st = res.stated_total
        if st is None:
            return False
        if not _has_month_scale_evidence(res, month, year, self.s.weekend_set):
            return False
        daily = sum((e.total or e.regular or e.overtime or 0.0) for e in res.entries)
        if daily <= 0:                                   # need real per-day evidence
            return False
        if abs(daily - float(st)) > self.s.consensus_printed_match_h:
            return False
        worked, _ = self._worked_total(res)
        return 8 <= float(st) <= self.s.consensus_ceiling_h and worked <= 23

    def _consensus_gate(self, key_a, key_b, month, year, act):
        """Decide from two derivations. Auto-accept only when they AGREE and all
        four locks pass; otherwise keep the better-supported read for review."""
        def usable(r):
            return bool(r is not None and (r.entries or r.weekly_totals
                                           or r.stated_total is not None))
        def evid(r):
            return r is not None and _has_month_scale_evidence(
                r, month, year, self.s.weekend_set)
        a_ok, b_ok = usable(key_a), usable(key_b)
        if not a_ok and not b_ok:
            return None
        # only one derivation -> keep it, but it is unconfirmed -> review
        if a_ok != b_ok:
            one = key_a if a_ok else key_b
            one.confidence = min(one.confidence, 0.7)
            one.verification = one.verification or "unverified"
            one.notes.append("consensus: only one derivation available; unconfirmed")
            act("Consensus", "single",
                f"one derivation only ({self._worked_total(one)[1]:g}h)", ok=False)
            return one

        aw, at = self._worked_total(key_a)
        bw, bt = self._worked_total(key_b)
        hi, lo = max(at, bt), min(at, bt)
        agree = abs(at - bt) <= self.s.consensus_agree_tolerance
        # LOCKS
        ratio_ok = hi <= 0 or lo >= self.s.consensus_ratio_veto * hi
        ceiling_ok = 8 < hi <= self.s.consensus_ceiling_h
        low_total_ok = (hi >= self.s.consensus_low_total_h) or (aw > 0 and bw > 0)
        vision_only = (not evid(key_a)) and (not evid(key_b))
        # carrier = the richer read (more day detail); prefer deterministic on a tie
        richer = key_a if len(key_a.entries) >= len(key_b.entries) else key_b

        if agree and ratio_ok and ceiling_ok and low_total_ok:
            # two keys agree + locks pass. If BOTH are vision reads it can only be
            # CONFIRMED_VISION_ONLY (correlated errors) -> review, not auto-accept.
            richer.confidence = max(richer.confidence, 0.9)
            richer.needs_llm = False
            richer.verification = "confirmed_vision_only" if vision_only else "confirmed"
            tag = "CONFIRMED (vision-only)" if vision_only else "CONFIRMED"
            richer.notes.append(
                f"consensus {tag}: Key A {at:g}h and Key B {bt:g}h agree "
                f"(<= {self.s.consensus_agree_tolerance:g}h)")
            act("Consensus", "confirmed",
                f"A {at:g}h == B {bt:g}h{' (vision-only)' if vision_only else ''}")
            return richer

        # disagreement / lock trip -> keep the better-supported read for review.
        # when the ratio veto fires, the higher read is far likelier correct.
        if not ratio_ok:
            chosen = key_a if at >= bt else key_b
        elif evid(key_a) and not evid(key_b):
            chosen = key_a
        elif evid(key_b) and not evid(key_a):
            chosen = key_b
        else:
            chosen = richer
        chosen.confidence = min(chosen.confidence, 0.65)      # -> needs_review
        chosen.verification = "unverified"
        chosen.notes.append(
            f"consensus DISAGREEMENT: Key A {at:g}h vs Key B {bt:g}h; needs review")
        act("Consensus", "disagree", f"A {at:g}h vs B {bt:g}h", ok=False)
        return chosen

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

        # DIRECT track: send the whole file to the model ladder, skip parsing.
        if self.s.is_direct:
            return self._process_direct(path, rel, month, year, client_hint,
                                        det, report, act, done)

        # CONSENSUS track: two independent derivations (deterministic + blind
        # model read) must agree before a number auto-accepts.
        if self.s.is_consensus:
            return self._process_consensus(path, rel, month, year, client_hint,
                                           det, report, act, done)

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

        # PERIOD RESOLVER: reconcile the requested month against the filename and
        # the file's own dates (2-of-3 majority). A confident disagreement is
        # flagged for review so a wrong-month file can't silently pollute totals.
        self._resolve_period(rel, raw, results, month, year, act)

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

        # PREMIUM PLUS: the Claude-baseline technique on existing models. When a
        # scan/photo came out UNDER-READ (OCR/deterministic missed most of it), do
        # a full-IMAGE vision re-read with the exhaustive prompt on the strong
        # existing model (gemini) and keep it if it's more plausible. This is what
        # recovers cases like Rajani (3h -> 176h) that plain premium under-reads.
        if self.s.is_premium_plus and raw.images and self.llm_norm.enabled:
            for i, res in enumerate(results):
                w, t = self._worked_total(res)
                under = (t <= 0
                         or (t < self.s.premium_plus_min_hours
                             and w < self.s.premium_plus_min_days)
                         or res.needs_llm or res.confidence < 0.5)
                if under:
                    better = self._premium_plus_vision(
                        path, rel, month, year, client_hint, det, res, act)
                    if better is not None:
                        results[i] = better

        done(self._agent_for(results[0].method) if results else "Normalizer")
        return results

    def _resolve_period(self, rel, raw, results, month, year, act) -> None:
        """Tag results whose own period disagrees with the requested month.

        Signals: the requested month, the filename month, and the dominant
        in-document date month. On a confident mismatch every result carries a
        note that the registry turns into a PERIOD_MISMATCH review flag. We never
        silently reprocess under a different month here -- the number stays tied
        to the requested period and is surfaced for a human to reroute.
        """
        try:
            blob = raw.text or ""
            if raw.tables:
                blob += "\n" + "\n".join(
                    " ".join("" if c is None else str(c) for c in row)
                    for t in raw.tables for row in t.rows)
            order = D.infer_date_order([blob], month, year)
            sheet_dates = D.collect_dates(blob, order, year)
            pr = D.resolve_target_period(month, year, filename=Path(rel).name,
                                         sheet_dates=sheet_dates)
        except Exception as exc:                       # never let this abort a file
            log.debug("period resolver failed for %s: %s", rel, exc)
            return
        if pr.mismatch:
            for res in results:
                res.period_mismatch = pr.note
            act("PeriodResolver", "period_mismatch",
                pr.note or pr.status, ok=False)
        else:
            act("PeriodResolver", "period_confirmed", f"{pr.year}-{pr.month:02d}")

    def _premium_plus_vision(self, path, rel, month, year, client_hint, det,
                             current, act):
        """Full-image vision re-read of an under-read scan, using the exhaustive
        prompt on the strong existing model. Keeps it only if more plausible."""
        from .direct.extractor import DirectExtractor
        from .ingest.excel import _name_hint
        if self._direct is None:
            self._direct = DirectExtractor(self.router, self.s)
        try:
            nh = _name_hint(Path(rel).stem)
        except Exception:
            nh = None
        try:
            out = self._direct.extract(path, rel, month, year, client_hint, nh,
                                       det.kind, act,
                                       ladder=self.s.premium_plus_vision_ladder)
        except Exception as exc:
            log.warning("premium-plus vision failed for %s: %s", rel, exc)
            return None
        if not out:
            return None
        cand = out[0]
        cw, ct = self._worked_total(cand)
        ow, ot = self._worked_total(current)
        # Keep the re-read ONLY when it's clearly MORE plausible than Premium's:
        #  * over-read fix: Premium read >230h (impossible) and the re-read lands in
        #    a sane 8-230h month -> take it (even though it's a lower number); or
        #  * under-read recovery: the re-read reaches at least ~a week of work
        #    (>=40h) AND beats Premium.
        # A suspiciously LOW re-read (<40h) never displaces Premium -- that is what
        # prevents the Suman 160->8 / Suganthan 144->15 regressions; those stay on
        # Premium's read and simply flag for review.
        plausible = cand.entries or cand.weekly_totals or cand.stated_total
        ok = (plausible and cw <= 24 and 0 < ct <= 230
              and (ot > 230 or (ct >= 40 and ct > ot)))
        if ok:
            cand.notes.append(
                f"premium-plus vision re-read: {ot:g}h/{ow}d -> {ct:g}h/{cw}d")
            act("VisionReader", "second_opinion",
                f"{ot:g}h -> {ct:g}h", model=cand.method.split(":", 1)[-1])
            return cand
        return current

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
