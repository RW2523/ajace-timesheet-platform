"""Direct track: send the WHOLE file to a vision LLM with one exhaustive prompt.

Every file becomes ONE request. The model reads the real document (PDF pages or
image) and returns the full mega-contract JSON (see prompts.DIRECT_MEGA_CONTRACT).
A cheap primary model runs first; on low confidence / self-check failure /
implausible reads it escalates up a ladder (nano -> mini -> gpt-5). The winning
JSON is mapped to the same NormResult the rest of the engine consumes, so the
registry, validator, agent-trace and report are all reused unchanged.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..llm.prompts import direct_extract_system
from ..llm.router import ModelRouter
from ..normalize.llm_normalizer import LLMNormalizer
from ..normalize.normalizer import NormResult
from ..schema import FileKind, RawExtraction, SourceRef
from ..settings import Settings, get_settings

log = logging.getLogger("tsengine.direct")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _worked_total(res: NormResult) -> tuple[int, float]:
    worked = sum(1 for e in res.entries if (e.total or 0) > 0)
    total = sum((e.total or 0) for e in res.entries)
    return worked, round(total, 2)


class DirectExtractor:
    """Reads one file with the model ladder and returns a list[NormResult]."""

    def __init__(self, router: ModelRouter, settings: Optional[Settings] = None):
        self.s = settings or get_settings()
        self.router = router
        self.client = router.client
        # reuse the canonical JSON->NormResult mapper (superset-compatible contract)
        self._mapper = LLMNormalizer(router, self.s)

    # -- file -> model input --------------------------------------------------
    def _as_model_input(self, path: Path) -> tuple[Optional[Path], list[Path]]:
        """Return (pdf_path, image_paths) to attach. Images go as image parts;
        everything else is converted to a single PDF (native model file input)."""
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            return None, [path]
        if ext == ".pdf":
            return path, []
        # xlsx / xls / docx / eml / csv -> PDF via the (already-tested) converter
        from ..preview import to_pdf
        pdf = to_pdf(path, self.s)
        if pdf:
            return Path(pdf), []
        return None, []

    def _min_raw(self, rel: str, kind: FileKind, name_hint: Optional[str]) -> RawExtraction:
        raw = RawExtraction(file=rel, kind=kind)
        raw.sources = [SourceRef(file=rel, extractor="direct")]
        if name_hint:
            raw.meta["name_hint"] = name_hint
        return raw

    # -- one model call -------------------------------------------------------
    def _call(self, model: str, system: str, pdf: Optional[Path],
              images: list[Path]) -> Optional[dict]:
        user = self.client.file_message(
            "Extract the full mega-contract JSON for this document now.",
            file_path=pdf, images=images)
        messages = [{"role": "system", "content": system}, user]
        try:
            with self.router._lock:
                self.router.calls += 1
            resp = self.client.chat(model, messages, temperature=0.0,
                                    max_tokens=8000, json_mode=True)
            self.router._record(model, resp.usage)
            from ..llm.client import _loads_lenient
            data = _loads_lenient(resp.text)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("direct call %s failed: %s", model, exc)
            self.router._dead.discard(model)   # transient; allow retry on next file
            return None

    # -- quality gate on one model's answer ----------------------------------
    def _accept(self, data: dict, res: NormResult) -> tuple[bool, str]:
        """Is this read good enough to stop the ladder? Returns (ok, reason).

        NOTE: a self-check mismatch is NOT an escalation trigger -- a genuine
        stated-total-vs-daily-sum discrepancy in the DOCUMENT won't be resolved by
        a bigger model, so it's routed to human review instead (see extract()).
        We only climb the ladder for things a stronger model can actually fix:
        missing data, low confidence, or an implausible read.
        """
        conf = float(data.get("confidence") or res.confidence or 0)
        worked, total = _worked_total(res)
        has_data = bool(res.entries or res.weekly_totals or res.stated_total is not None)
        if not has_data:
            return False, "no usable data"
        if conf < self.s.direct_min_confidence:
            return False, f"confidence {conf:.2f} < {self.s.direct_min_confidence}"
        if worked > 23 or total > 300 or (res.entries and total < 8):
            return False, f"implausible ({total}h/{worked}d)"
        return True, f"confidence {conf:.2f}, {total}h/{worked}d"

    # -- main ----------------------------------------------------------------
    def extract(self, path: Path, rel: str, month: int, year: int,
                client_hint: Optional[str], name_hint: Optional[str],
                kind: FileKind, act: Optional[Callable] = None
                ) -> Optional[list[NormResult]]:
        act = act or (lambda *a, **k: None)
        path = Path(path)
        pdf, images = self._as_model_input(path)
        if pdf is None and not images:
            act("DirectReader", "rejected", "could not convert file for the model",
                ok=False)
            return None
        system = direct_extract_system(month, year)
        raw = self._min_raw(rel, kind, name_hint)

        best: Optional[NormResult] = None
        best_data: dict = {}
        totals_seen: list[float] = []
        ladder = self.s.direct_ladder

        for i, model in enumerate(ladder):
            data = self._call(model, system, pdf, images)
            if data is None:
                act("DirectReader", "escalated", f"{model}: no/invalid JSON",
                    model=model, ok=False)
                continue

            # not a timesheet -> stop, signal skip
            if data.get("is_timesheet") is False or \
                    data.get("document_type") in ("invoice", "other"):
                act("DirectReader", "rejected",
                    f"{model}: document_type={data.get('document_type')}",
                    model=model, ok=False)
                return None

            res = self._mapper._from_contract(
                data, raw, month, year, client_hint,
                method=f"direct:{model}", quality=raw.quality, order="MDY")
            self._absorb_direct_fields(res, data)
            _, tot = _worked_total(res)
            if tot:
                totals_seen.append(tot)

            ok, reason = self._accept(data, res)
            act("DirectReader", "accepted" if ok else "escalated",
                f"{reason}", model=model, ok=ok)
            if ok:
                best, best_data = res, data   # accepted read always wins
                break                         # stop climbing the ladder
            # rejected: keep the most-complete read as a fallback if none accepts
            if best is None or _worked_total(res)[1] > _worked_total(best)[1]:
                best, best_data = res, data

        if best is None:
            act("DirectReader", "rejected", "all models failed", ok=False)
            return None

        # cross-model agreement flag (>=2 reads disagreeing on the month total)
        if len(totals_seen) >= 2:
            spread = max(totals_seen) - min(totals_seen)
            if spread > self.s.direct_agreement_tolerance:
                best.notes.append(
                    f"models disagreed on monthly total by {spread:g}h "
                    f"({', '.join(f'{t:g}' for t in totals_seen)})")
                best.needs_llm = True   # -> routed to human review by the validator
        return [best]

    def _absorb_direct_fields(self, res: NormResult, data: dict) -> None:
        """Fold the mega-contract's extra fields onto the NormResult so the
        validator/registry/UI can use them (period, self-check, ambiguities)."""
        res.confidence = float(data.get("confidence") or res.confidence or 0.65)
        for a in (data.get("ambiguities") or []):
            res.notes.append(f"ambiguity: {a}")
        sc = data.get("self_check") or {}
        discs = sc.get("discrepancies") or []
        for d in discs:
            res.notes.append(f"self-check: {d}")
        # a self-check discrepancy the model couldn't resolve -> route to a human
        # (validator turns needs_llm into review), but do NOT burn a bigger model.
        if (sc.get("matches_stated_total") is False or
                sc.get("per_day_reg_ot_consistent") is False) and discs:
            res.needs_llm = True
        # prefer the model's explicit month total when the daily sum is silent
        if res.stated_total is None:
            st = data.get("stated_total")
            if st is not None:
                try:
                    res.stated_total = float(st)
                except (TypeError, ValueError):
                    pass
