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
        self._strong_used = 0      # per-batch budget for the strongest rung

    # -- file -> model input --------------------------------------------------
    def _as_model_input(self, path: Path
                        ) -> tuple[Optional[Path], list[Path], str]:
        """Return (pdf_path, image_paths, extra_text) to attach.

        Images go as image parts (lightly enhanced for faint scans); PDFs go as a
        native file part. Emails are NOT flattened to PDF -- the body text (which
        often carries the weekly table / 'Approved N hours' line) travels as text
        and the attachments as their own parts. Office docs convert to PDF, with
        an embedded-image fallback for docx that won't convert.
        """
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            return None, [self._prep_image(path)], ""
        if ext == ".pdf":
            return path, [], ""
        if ext == ".eml":
            pdf, images, body = self._eml_input(path)
            if pdf or images or body:
                return pdf, images, body
        # xlsx / xls / docx / csv -> PDF via the (already-tested) converter
        from ..preview import to_pdf
        pdf = to_pdf(path, self.s)
        if pdf:
            return Path(pdf), [], ""
        if ext == ".docx":                     # conversion failed -> embedded images
            imgs = self._docx_images(path)
            if imgs:
                return None, imgs, ""
        return None, [], ""

    def _eml_input(self, path: Path) -> tuple[Optional[Path], list[Path], str]:
        """Email -> (pdf attachment, image attachments, body text). The body is
        sent as TEXT so a pasted weekly table / approval line is actually read."""
        import email
        import email.policy
        import re as _re
        import tempfile
        pdf: Optional[Path] = None
        images: list[Path] = []
        body = ""
        try:
            msg = email.message_from_bytes(path.read_bytes(),
                                           policy=email.policy.default)
        except Exception as exc:
            log.warning("direct eml parse failed for %s: %s", path.name, exc)
            return None, [], ""
        hdr = "\n".join(f"{k}: {msg.get(k)}" for k in ("Subject", "From", "Date")
                        if msg.get(k))
        try:
            b = msg.get_body(preferencelist=("plain", "html"))
            if b is not None:
                body = b.get_content()
                if b.get_content_type() == "text/html":
                    body = _re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass
        tmp = Path(tempfile.mkdtemp(prefix="ts_direct_eml_"))
        for part in msg.iter_attachments():
            fn = part.get_filename() or "attachment"
            safe = _re.sub(r"[^A-Za-z0-9._-]", "_", fn)
            try:
                data = part.get_payload(decode=True)
                if not data:
                    continue
                p = tmp / safe
                p.write_bytes(data)
                ext = p.suffix.lower()
                if ext == ".pdf" and pdf is None:
                    pdf = p
                elif ext in IMAGE_EXTS:
                    images.append(self._prep_image(p))
            except Exception:
                continue
        text = (hdr + "\n\n" + (body or "")).strip()
        return pdf, images, text[:20000]

    def _docx_images(self, path: Path) -> list[Path]:
        """Embedded images from a docx that would not convert to PDF (screenshots
        pasted into Word are the common timesheet case)."""
        import tempfile
        import zipfile
        out: list[Path] = []
        try:
            tmp = Path(tempfile.mkdtemp(prefix="ts_direct_docx_"))
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.startswith("word/media/") and \
                            Path(name).suffix.lower() in IMAGE_EXTS:
                        p = tmp / Path(name).name
                        p.write_bytes(z.read(name))
                        out.append(self._prep_image(p))
        except Exception as exc:
            log.warning("docx image fallback failed for %s: %s", path.name, exc)
        return out[:10]

    def _prep_image(self, path: Path) -> Path:
        """Light enhancement for faint scans/screenshots: autocontrast + 2x
        upscale when small. Falls back to the original on any failure."""
        if not self.s.direct_preprocess_images:
            return path
        try:
            import tempfile

            from PIL import Image, ImageOps
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if min(im.size) < 1400:                       # small -> 2x upscale
                im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
            im = ImageOps.autocontrast(im, cutoff=1)
            out = Path(tempfile.mkdtemp(prefix="ts_direct_img_")) / \
                (path.stem + "_prep.png")
            im.save(out, "PNG")
            return out
        except Exception:
            return path

    def _min_raw(self, rel: str, kind: FileKind, name_hint: Optional[str]) -> RawExtraction:
        raw = RawExtraction(file=rel, kind=kind)
        raw.sources = [SourceRef(file=rel, extractor="direct")]
        if name_hint:
            raw.meta["name_hint"] = name_hint
        return raw

    # -- one model call -------------------------------------------------------
    def _user_text(self, extra_text: str) -> str:
        base = "Extract the full mega-contract JSON for this document now."
        if extra_text:
            return (base + "\n\nThe document is an EMAIL; its body text follows "
                    "(attachments are included as file/image parts):\n\n" + extra_text)
        return base

    def _call(self, model: str, system: str, pdf: Optional[Path],
              images: list[Path], extra_text: str = ""
              ) -> tuple[Optional[dict], str]:
        """One read. Returns (parsed_json_or_None, raw_response_text)."""
        user = self.client.file_message(self._user_text(extra_text),
                                        file_path=pdf, images=images)
        messages = [{"role": "system", "content": system}, user]
        return self._chat_json(model, messages)

    def _chat_json(self, model: str, messages: list[dict]
                   ) -> tuple[Optional[dict], str]:
        try:
            with self.router._lock:
                self.router.calls += 1
            resp = self.client.chat(model, messages, temperature=0.0,
                                    max_tokens=8000, json_mode=True)
            self.router._record(model, resp.usage)
            from ..llm.client import _loads_lenient
            data = _loads_lenient(resp.text)
            return (data if isinstance(data, dict) else None), (resp.text or "")
        except Exception as exc:
            log.warning("direct call %s failed: %s", model, exc)
            self.router._dead.discard(model)   # transient; allow retry on next file
            return None, ""

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
        # suspiciously SPARSE: a grid read under 40h with no printed total to
        # corroborate it is far more often an under-read (faint scan, partial
        # page) than a genuine sub-40h month -> climb to a stronger reader.
        # A sheet that PRINTS a small total (a true part-time month) is exempt.
        if res.entries and total < 40 and res.stated_total is None:
            return False, f"suspiciously sparse ({total}h/{worked}d, no printed total)"
        return True, f"confidence {conf:.2f}, {total}h/{worked}d"

    # -- main ----------------------------------------------------------------
    def extract(self, path: Path, rel: str, month: int, year: int,
                client_hint: Optional[str], name_hint: Optional[str],
                kind: FileKind, act: Optional[Callable] = None,
                ladder: Optional[list[str]] = None
                ) -> Optional[list[NormResult]]:
        act = act or (lambda *a, **k: None)
        path = Path(path)
        pdf, images, extra_text = self._as_model_input(path)
        if pdf is None and not images and not extra_text:
            act("DirectReader", "rejected", "could not convert file for the model",
                ok=False)
            return None
        system = direct_extract_system(month, year)
        raw = self._min_raw(rel, kind, name_hint)

        best: Optional[NormResult] = None
        best_data: dict = {}
        totals_seen: list[float] = []
        repaired = False
        ladder = ladder or self.s.direct_ladder

        for i, model in enumerate(ladder):
            # per-batch budget on the strongest rung: past it, keep the best
            # cheap read (flagged) instead of burning gpt-5 on every hard file.
            if model == self.s.direct_fallback2_model and \
                    self._strong_used >= self.s.direct_strong_budget:
                act("DirectReader", "skipped",
                    f"{model}: strong-model budget exhausted "
                    f"({self._strong_used}/{self.s.direct_strong_budget})",
                    model=model, ok=False)
                continue
            data, raw_text = self._call(model, system, pdf, images, extra_text)
            if model == self.s.direct_fallback2_model and data is not None:
                self._strong_used += 1
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

            res = self._map(data, raw, month, year, client_hint, model, act)

            # ARITHMETIC REPAIR (one shot per file): the code-computed sum of the
            # model's own entries disagrees with what the model claimed -- an
            # internal reading slip a follow-up usually fixes.
            if self.s.direct_repair and not repaired and \
                    self._needs_repair(data, res):
                repaired = True
                fixed = self._repair(model, system, pdf, images, extra_text,
                                     raw_text, data, res, raw, month, year,
                                     client_hint, act)
                if fixed is not None:
                    res, data = fixed

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

        # cross-model disagreement, graded by size: a small spread (<= block
        # threshold) on two plausible reads asks for a human GLANCE (review), not
        # a hard block; only a real divergence blocks. Either way the second
        # model already served as the independent opinion -> verify is redundant.
        spread = 0.0
        if len(totals_seen) >= 2:
            spread = max(totals_seen) - min(totals_seen)
            if spread > self.s.direct_block_spread:
                best.notes.append(
                    f"models disagreed on monthly total by {spread:g}h "
                    f"({', '.join(f'{t:g}' for t in totals_seen)})")
                best.needs_llm = True   # -> blocked by the validator
            elif spread > self.s.direct_agreement_tolerance:
                best.notes.append(
                    f"models differed slightly on the monthly total ({spread:g}h) "
                    "-- flagged for review")
                best.confidence = min(best.confidence, 0.8)   # -> needs_review

        # BLIND self-verification: a cheap second read re-derives just the monthly
        # total (never shown the first read's number). Agreement corroborates the
        # read -> confirmed; disagreement flags a possibly-wrong confident read.
        if spread <= self.s.direct_agreement_tolerance and \
                self._should_verify(best, images, repaired):
            _, bt = _worked_total(best)
            if bt > 0:
                self._verify(best, bt, pdf, images, month, year, act)

        return [best]

    # -- mapping + arithmetic repair -------------------------------------------
    def _map(self, data: dict, raw: RawExtraction, month: int, year: int,
             client_hint: Optional[str], model: str, act: Callable) -> NormResult:
        res = self._mapper._from_contract(
            data, raw, month, year, client_hint,
            method=f"direct:{model}", quality=raw.quality, order="MDY")
        self._absorb_direct_fields(res, data)
        self._dedupe_entries(res, act)
        # a printed total implausibly small next to a full daily grid is a
        # misread of some other box ("Total hours in the day 8:00"), not the
        # month's total -- discard it rather than raise a false TOTAL_MISMATCH
        # (or trigger a pointless repair round on the phantom mismatch).
        _, code_sum = _worked_total(res)
        if res.stated_total is not None and res.entries and code_sum >= 40 \
                and res.stated_total < 40 and res.stated_total < 0.5 * code_sum:
            res.notes.append(
                f"discarded implausible printed total {res.stated_total:g}h "
                f"(the daily grid sums to {code_sum:g}h)")
            res.stated_total = None
        return res

    @staticmethod
    def _dedupe_entries(res: NormResult, act: Callable) -> None:
        """COUNT DAYS ONCE, enforced in code: the same date emitted twice (a
        re-shown page) collapses to one entry. Identical values merge silently;
        conflicting values keep the first and flag the conflict."""
        seen: dict = {}
        out = []
        conflicts = 0
        for e in res.entries:
            k = e.date
            if k not in seen:
                seen[k] = e
                out.append(e)
            elif (e.total or 0) != (seen[k].total or 0):
                conflicts += 1
        if len(out) != len(res.entries):
            dropped = len(res.entries) - len(out)
            res.entries = out
            note = f"deduped {dropped} repeated day entr{'y' if dropped == 1 else 'ies'}"
            if conflicts:
                note += f" ({conflicts} conflicting -- kept the first reading)"
                res.needs_llm = res.needs_llm or conflicts > 2
            res.notes.append(note)
            act("DirectReader", "deduped", note)

    def _needs_repair(self, data: dict, res: NormResult) -> bool:
        """The model's own claims disagree with the CODE-computed sum of its
        entries -> an internal slip worth one corrective round. A genuine
        document discrepancy (entries consistent, printed total different) is
        NOT repaired -- that goes to review."""
        if not res.entries:
            return False
        _, code_sum = _worked_total(res)
        tol = self.s.direct_agreement_tolerance
        sc = data.get("self_check") or {}
        try:
            claimed = float(sc.get("sum_of_daily_totals"))
        except (TypeError, ValueError):
            claimed = None
        if claimed is not None and abs(claimed - code_sum) > tol:
            return True
        # model says its sum matches the printed total, but code disagrees
        if sc.get("matches_stated_total") and res.stated_total is not None \
                and abs(float(res.stated_total) - code_sum) > tol:
            return True
        return False

    def _repair(self, model: str, system: str, pdf: Optional[Path],
                images: list[Path], extra_text: str, raw_text: str, data: dict,
                res: NormResult, raw: RawExtraction, month: int, year: int,
                client_hint: Optional[str], act: Callable
                ) -> Optional[tuple[NormResult, dict]]:
        from ..llm.prompts import direct_repair_message
        _, code_sum = _worked_total(res)
        worked, _ = _worked_total(res)
        sc = data.get("self_check") or {}
        msg = direct_repair_message(code_sum, worked,
                                    sc.get("sum_of_daily_totals"),
                                    data.get("stated_total"))
        messages = [
            {"role": "system", "content": system},
            self.client.file_message(self._user_text(extra_text),
                                     file_path=pdf, images=images),
            {"role": "assistant", "content": raw_text[:30000]},
            {"role": "user", "content": msg},
        ]
        fixed_data, _ = self._chat_json(model, messages)
        if fixed_data is None or not isinstance(fixed_data, dict):
            act("DirectRepair", "failed", "no corrected JSON", model=model, ok=False)
            return None
        fixed = self._map(fixed_data, raw, month, year, client_hint, model, act)
        if not fixed.entries:
            act("DirectRepair", "rejected", "correction lost the entries",
                model=model, ok=False)
            return None
        _, new_sum = _worked_total(fixed)
        fixed.notes.append(
            f"arithmetic repair: entries re-read ({code_sum:g}h -> {new_sum:g}h)")
        act("DirectRepair", "corrected", f"{code_sum:g}h -> {new_sum:g}h",
            model=model)
        return fixed, fixed_data

    # -- verify scheduling -----------------------------------------------------
    def _should_verify(self, best: NormResult, images: list[Path],
                       repaired: bool) -> bool:
        # verify belongs to the DIRECT flow only: premium+/consensus reuse this
        # extractor for re-reads but bring their own corroboration (the
        # plausibility guard / Key A), so an extra call there is wasted cost.
        if not self.s.is_direct:
            return False
        if not self.s.direct_verify or best.needs_llm:
            return False
        mode = (self.s.direct_verify_mode or "auto").strip().lower()
        if mode == "off":
            return False
        if mode == "always":
            return True
        # auto: spend the verify call only on GRAY-ZONE reads
        worked, total = _worked_total(best)
        gray = (best.confidence < 0.9          # model itself unsure
                or bool(images)                # scan/photo input
                or repaired                    # needed an arithmetic fix
                or (0 < total < 60 and worked < 8))   # sparse for a month
        return gray

    # -- blind verification pass ---------------------------------------------
    def _verify_model_for(self, best: NormResult) -> str:
        """A verify model in a DIFFERENT family than the read being checked, so the
        two don't share a bias and 'agree' on the same mistake (the false-confirm
        seen on biweekly/stray-row files). If the configured verify model is the
        one that produced this read, decorrelate with a stronger/other model."""
        best_model = (best.method or "").split(":", 1)[-1]
        vm = self.s.direct_verify_model
        if vm != best_model:
            return vm
        for alt in (self.s.consensus_tiebreak_ladder + [self.s.direct_fallback2_model]):
            if alt and alt != best_model:
                return alt
        return vm

    def _verify(self, best: NormResult, primary_total: float, pdf: Optional[Path],
                images: list[Path], month: int, year: int, act: Callable) -> None:
        from ..llm.client import _loads_lenient
        from ..llm.prompts import direct_verify_system
        model = self._verify_model_for(best)
        msgs = [{"role": "system", "content": direct_verify_system(month, year)},
                self.client.file_message("Give the verification JSON now.",
                                         file_path=pdf, images=images)]
        try:
            with self.router._lock:
                self.router.calls += 1
            # reasoning models (mini/gpt-5) burn part of the budget on reasoning
            # tokens; 300 truncated the tiny verify JSON (finish_reason=length).
            resp = self.client.chat(model, msgs, temperature=0.0, max_tokens=2000,
                                    json_mode=True)
            self.router._record(model, resp.usage)
            data = _loads_lenient(resp.text)
        except Exception as exc:
            log.warning("direct verify failed: %s", exc)
            return
        if not isinstance(data, dict):
            return
        try:
            vt = float(data.get("monthly_total"))
        except (TypeError, ValueError):
            return
        if abs(vt - primary_total) <= self.s.direct_agreement_tolerance:
            best.verification = "confirmed"            # two blind reads agree
            best.confidence = max(best.confidence, 0.9)
            best.notes.append(
                f"verified: an independent re-read agrees ({primary_total:g}h ≈ {vt:g}h)")
            act("DirectVerifier", "confirmed",
                f"{primary_total:g}h ≈ {vt:g}h", model=model)
        else:
            best.confidence = min(best.confidence, 0.6)  # -> needs_review
            best.notes.append(
                f"verification DISAGREED: primary {primary_total:g}h vs re-read {vt:g}h "
                "-- please confirm")
            act("DirectVerifier", "disagree",
                f"{primary_total:g}h vs {vt:g}h", model=model, ok=False)

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
        # A self-check discrepancy is a "give it a glance" signal, NOT a block. If
        # it reflects a real stated-total-vs-sum gap, the validator's own
        # TOTAL_MISMATCH check will surface it as a warning (-> needs_review). We do
        # NOT set needs_llm here (that maps to "blocked") -- only a genuine
        # cross-model disagreement or a hard validator error blocks a record.
        # prefer the model's explicit month total when the daily sum is silent
        if res.stated_total is None:
            st = data.get("stated_total")
            if st is not None:
                try:
                    res.stated_total = float(st)
                except (TypeError, ValueError):
                    pass
