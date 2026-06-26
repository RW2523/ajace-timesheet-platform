"""LLM-backed normalization (text and vision), routed through OpenRouter.

Used only when deterministic extraction is insufficient (or policy=always). The
model returns the canonical JSON contract from ``prompts`` which we convert into
the same ``NormResult`` the deterministic path produces -- so the rest of the
pipeline is agnostic to how a record was obtained.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Optional

from ..llm.prompts import (dump_tables_for_prompt, normalize_text_messages,
                           vision_normalize_prompt)
from ..llm.router import ModelRouter
from ..schema import (ExtractionQuality, FileKind, RawExtraction, SourceRef,
                      WeeklyTotal)
from ..settings import Settings, get_settings
from . import dates as D
from . import hours as H
from .normalizer import DayEntry, NormResult


def _most_common(values: list) -> Optional[str]:
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else None


class LLMNormalizer:
    def __init__(self, router: ModelRouter, settings: Optional[Settings] = None):
        self.router = router
        self.s = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.router.enabled

    @staticmethod
    def _has_data(res: Optional[NormResult]) -> bool:
        if res is None:
            return False
        if any(any(v is not None for v in (e.regular, e.overtime, e.total))
               for e in res.entries):
            return True
        return any(w.total_hours is not None or w.regular_hours is not None
                   for w in res.weekly_totals)

    def normalize(self, raw: RawExtraction, month: int, year: int,
                  client_hint: Optional[str] = None) -> Optional[NormResult]:
        if not self.enabled:
            return None
        # Image-bearing documents (scanned PDFs, photos, image-only DOCX) are read
        # page-by-page with vision and aggregated -- this is what makes multi-week
        # stacked grids sum correctly instead of returning only the first week.
        image_doc = bool(raw.images) and raw.kind in (
            FileKind.PDF_SCANNED, FileKind.IMAGE, FileKind.DOCX)
        if image_doc:
            # Flow: per-page VISION grounded in the layout-OCR text. The OCR
            # ("Layout Finder") supplies exact cell values so the model can't
            # hallucinate hours into blank/0 (e.g. weekend) cells, while the image
            # supplies layout for calendar/odd grids. When OCR is too poor to trust
            # (handwriting/low-quality scan) the grounding is dropped and the model
            # reads the image freely. OCR-only text is the final fallback.
            primary = self._vision_consistent(raw, month, year, client_hint)
            if self._has_data(primary):
                return primary
            if raw.text.strip():
                alt = self._text_pages_or_single(raw, month, year, client_hint)
                if self._has_data(alt):
                    return alt
            return primary

        # Native-text documents. Multi-page text (one weekly grid per page) is
        # normalized page-by-page and aggregated, so a 5-week PDF doesn't collapse
        # to a single week. A vision fallback covers garbage / image-only pages.
        primary = self._text_pages_or_single(raw, month, year, client_hint)
        if self._has_data(primary):
            return primary
        if raw.images:
            alt = self._vision_per_page(raw, month, year, client_hint)
            if self._has_data(alt):
                return alt
        return primary

    def _text_pages_or_single(self, raw, month, year, client_hint) -> Optional[NormResult]:
        """Per-page text normalization when the text carries page markers
        (one grid per page); otherwise a single text call."""
        import re as _re
        chunks = _re.split(r"-{3,}\s*page\s+\d+[^\n]*-{3,}", raw.text)
        chunks = [c.strip() for c in chunks if len(c.strip()) > 40]
        if len(chunks) >= 2:
            return self._text_per_page(raw, chunks, month, year, client_hint)
        return self._text(raw, month, year, client_hint)

    def _page_call(self, task, messages, raw, month, year, client_hint, order,
                   quality, label):
        """One page's LLM call, retried once if it yields no data (model variance
        on a hard page would otherwise silently drop that page's whole week)."""
        last = (None, None)
        for _ in range(2):
            out = self.router.run(task, messages)
            if not out.ok or not isinstance(out.data, dict):
                continue
            pr = self._from_contract(out.data, raw, month, year, client_hint,
                                     method=f"{label}:{out.model}", quality=quality,
                                     order=order)
            last = (pr, out.model)
            if pr.entries or pr.weekly_totals or pr.stated_total is not None:
                return pr, out.model
        return last

    def _parallel_pages(self, page_args):
        """Run each page's _page_call concurrently (multi-page files are the
        runtime bottleneck). Returns (pr, model) per page, in input order."""
        import concurrent.futures
        if len(page_args) <= 1:
            return [self._page_call(*a) for a in page_args]
        workers = min(4, len(page_args))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(lambda a: self._page_call(*a), page_args))

    def _text_per_page(self, raw, page_chunks, month, year, client_hint
                       ) -> Optional[NormResult]:
        order = self._infer_order(raw, month, year)
        by_date: dict[dt.date, DayEntry] = {}
        weekly: list[WeeklyTotal] = []
        names, clients, projects, notes, confs = [], [], [], [], []
        used, model = 0, None
        page_args = [("normalize", normalize_text_messages(chunk, "", month, year),
                      raw, month, year, client_hint, order, raw.quality, "llm_text")
                     for chunk in page_chunks[:12]]
        for pr, m in self._parallel_pages(page_args):
            if pr is None:
                continue
            model = m
            used += 1
            if pr.employee_name:
                names.append(pr.employee_name)
            if pr.client:
                clients.append(pr.client)
            if pr.project:
                projects.append(pr.project)
            notes.extend(pr.notes)
            confs.append(pr.confidence)
            weekly.extend(pr.weekly_totals)
            for e in pr.entries:
                cur = by_date.get(e.date)
                if cur is None or (e.total or 0) > (cur.total or 0):
                    by_date[e.date] = e
        if used == 0:
            return None
        res = NormResult(file=raw.file, method=f"llm_text_pages:{model}",
                         quality=raw.quality)
        res.entries = [by_date[d] for d in sorted(by_date)]
        res.weekly_totals = weekly
        res.employee_name = _most_common(names) or raw.meta.get("name_hint")
        res.client = _most_common(clients) or client_hint
        res.project = _most_common(projects)
        res.notes = notes
        res.confidence = round(sum(confs) / len(confs), 2) if confs else 0.6
        return res

    @staticmethod
    def _ocr_by_page(text: str) -> dict:
        """Map page number -> that page's OCR text (for grounding vision)."""
        import re as _re
        out: dict = {}
        marks = list(_re.finditer(r"-{3,}\s*page\s+(\d+)[^\n]*-{3,}", text))
        if marks:
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
                out[int(m.group(1))] = text[m.end():end].strip()
        elif text.strip():
            out[1] = text.strip()
        return out

    def _vision_consistent(self, raw, month, year, client_hint) -> Optional[NormResult]:
        """Document-level self-consistency: if a multi-page scan comes back with
        suspiciously low day-coverage (a sign some pages were under-read by the
        model on this pass), re-extract once and keep the higher-coverage result.
        This neutralises run-to-run model variance on hard multi-week scans."""
        res = self._vision_per_page(raw, month, year, client_hint)
        npages = len(raw.images)
        if not self._has_data(res) or npages < 3:
            return res
        worked = sum(1 for e in res.entries if (e.total or 0) > 0)
        expected = min(npages * 5, 22)          # ~5 workdays per weekly page, capped
        if worked >= 0.6 * expected:
            return res                          # coverage looks complete
        cover = lambda r: sum((e.total or 0) for e in r.entries) if r else 0
        retry = self._vision_per_page(raw, month, year, client_hint)
        if self._has_data(retry) and cover(retry) > cover(res):
            retry.notes.append("re-extracted: first pass had low page coverage")
            return retry
        return res

    # -- per-page vision aggregation (grounded in OCR) -----------------------
    def _vision_per_page(self, raw, month, year, client_hint) -> Optional[NormResult]:
        order = self._infer_order(raw, month, year)
        # Only ground the model in OCR when the OCR is good enough to trust; for
        # poor OCR (handwriting/low quality) grounding would suppress real reads.
        ocr_conf = float(raw.meta.get("ocr_confidence") or 0.0)
        ground = ocr_conf >= self.s.ocr_ground_min_confidence
        ocr_by_page = self._ocr_by_page(raw.text) if ground else {}
        by_date: dict[dt.date, DayEntry] = {}
        weekly: list[WeeklyTotal] = []
        names: list[str] = []
        clients: list[str] = []
        projects: list[str] = []
        notes: list[str] = []
        confs: list[float] = []
        used = 0
        model = None
        page_args = []
        for idx, im in enumerate(raw.images[:12]):  # cap pages per document
            prompt = vision_normalize_prompt(month, year)
            page = getattr(im.source, "page", None) or (idx + 1)
            ocr = ocr_by_page.get(page) or (next(iter(ocr_by_page.values()))
                                            if len(ocr_by_page) == 1 else "")
            if ocr:
                # Ground the vision model in the actual OCR so it reads exact cell
                # values and does NOT invent hours into blank/0 (e.g. weekend) cells.
                prompt += ("\n\nLocal OCR of THIS page -- use it for the EXACT numeric "
                           "cell values and do NOT invent hours for cells that are "
                           "blank or 0 in the OCR:\n```\n" + ocr[:3500] + "\n```")
            msg = self.router.client.vision_message(prompt, [im.path])
            page_args.append(("vision", [msg], raw, month, year, client_hint,
                              order, ExtractionQuality.VISION, "llm_vision"))
        for pr, m in self._parallel_pages(page_args):
            if pr is None:
                continue
            model = m
            used += 1
            if pr.employee_name:
                names.append(pr.employee_name)
            if pr.client:
                clients.append(pr.client)
            if pr.project:
                projects.append(pr.project)
            notes.extend(pr.notes)
            confs.append(pr.confidence)
            weekly.extend(pr.weekly_totals)
            for e in pr.entries:
                # different weeks never share a date; if a date repeats across
                # pages keep the richer (higher-total) reading to avoid double count
                cur = by_date.get(e.date)
                if cur is None or (e.total or 0) > (cur.total or 0):
                    if e.source:
                        e.source.extractor = "llm_vision"
                    by_date[e.date] = e
        if used == 0:
            return None
        res = NormResult(file=raw.file, method=f"llm_vision_pages:{model}",
                         quality=ExtractionQuality.VISION)
        res.entries = [by_date[d] for d in sorted(by_date)]
        res.weekly_totals = weekly
        res.employee_name = _most_common(names) or raw.meta.get("name_hint")
        res.client = _most_common(clients) or client_hint
        res.project = _most_common(projects)
        res.notes = notes
        res.confidence = round(sum(confs) / len(confs), 2) if confs else 0.6
        return res

    @staticmethod
    def _infer_order(raw, month, year) -> str:
        return D.infer_date_order(
            [raw.text] + [str(c) for t in raw.tables for r in t.rows for c in r],
            month, year)

    # -- text/table path ------------------------------------------------------
    def _text(self, raw, month, year, client_hint) -> Optional[NormResult]:
        tables_dump = dump_tables_for_prompt(raw.tables)
        msgs = normalize_text_messages(raw.text, tables_dump, month, year)
        out = self.router.run("normalize", msgs)
        if not out.ok:
            return None
        return self._from_contract(out.data, raw, month, year, client_hint,
                                   method=f"llm_text:{out.model}",
                                   quality=raw.quality,
                                   order=self._infer_order(raw, month, year))

    # -- vision path ----------------------------------------------------------
    def _vision(self, raw, month, year, client_hint) -> Optional[NormResult]:
        prompt = vision_normalize_prompt(month, year)
        img_paths = [im.path for im in raw.images][:8]   # cap pages per call
        msg = self.router.client.vision_message(prompt, img_paths)
        out = self.router.run("vision", [msg])
        if not out.ok:
            return None
        res = self._from_contract(out.data, raw, month, year, client_hint,
                                  method=f"llm_vision:{out.model}",
                                  quality=ExtractionQuality.VISION,
                                  order=self._infer_order(raw, month, year))
        for e in res.entries:
            if e.source:
                e.source.extractor = "llm_vision"
        return res

    # -- contract -> NormResult ----------------------------------------------
    def _from_contract(self, data, raw, month, year, client_hint, method, quality,
                       order: str = "MDY") -> NormResult:
        res = NormResult(file=raw.file, method=method, quality=quality)
        if not isinstance(data, dict):
            res.needs_llm = True
            res.notes.append("LLM returned non-object payload")
            return res
        res.employee_name = data.get("employee_name") or raw.meta.get("name_hint")
        res.employee_id = data.get("employee_id")
        res.client = data.get("client") or client_hint
        res.project = data.get("project")
        res.stated_total = _num(data.get("stated_total"))
        res.confidence = float(data.get("confidence") or 0.65)
        for n in (data.get("notes") or []):
            res.notes.append(str(n))

        src = raw.sources[0] if raw.sources else SourceRef(file=raw.file)
        for ent in (data.get("entries") or []):
            # contract asks for ISO (parsed first); fall back to the per-file
            # inferred order if the model echoed a D/M/Y or M/D/Y source date.
            d = D.parse_date(ent.get("date"), order, year)
            if not D.in_target_month(d, month, year):
                continue
            res.entries.append(DayEntry(
                date=d,
                regular=_num(ent.get("regular_hours")),
                overtime=_num(ent.get("overtime_hours")),
                total=_num(ent.get("total_hours")),
                project=ent.get("project") or res.project,
                note=ent.get("note"),
                raw=ent.get("raw"),
                source=SourceRef(file=raw.file, page=getattr(src, "page", None),
                                 extractor=method.split(":")[0]),
            ))
        for wk in (data.get("weekly_totals") or []):
            ws = D.parse_date(wk.get("week_start"), order, year)
            we = D.parse_date(wk.get("week_end"), order, year)
            if not ws and not we:
                continue
            if ws and not we:
                we = ws + dt.timedelta(days=6)
            if we and not ws:
                ws = we - dt.timedelta(days=6)
            res.weekly_totals.append(WeeklyTotal(
                week_start=ws, week_end=we,
                regular_hours=_num(wk.get("regular_hours")),
                overtime_hours=_num(wk.get("overtime_hours")),
                total_hours=_num(wk.get("total_hours")),
                in_month_fraction=round(D.week_overlap_fraction(ws, we, month, year), 3),
                sources=[SourceRef(file=raw.file, extractor=method.split(":")[0])]))
        # derive r/o/t consistency
        for e in res.entries:
            e.regular, e.overtime, e.total = H.split_regular_overtime(
                e.total, e.regular, e.overtime)
        return res


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return H.parse_hours(v)
