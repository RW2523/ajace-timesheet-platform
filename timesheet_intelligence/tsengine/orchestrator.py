"""The orchestrator: inspect a file, pick the cheapest sufficient extraction
path, and return a ``RawExtraction``.

This is the "subagent dispatcher" of the engine -- each ingest module is a
specialized extraction subagent and the orchestrator routes by detected format
and quality:

    excel/csv          -> structured spreadsheet readers
    native PDF         -> text + table extraction (no OCR)
    scanned PDF/image  -> rasterize + OCR (+ vision later)
    docx               -> doc parser, falling back to embedded-image OCR
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .ingest import detect as _detect
from .ingest import (csv_ingest, docx_ingest, eml_ingest, excel, image,
                     pdf_native, pdf_scanned)
from .ingest.detect import DetectedFile
from .schema import ExtractionQuality, FileKind, RawExtraction
from .settings import Settings, get_settings

log = logging.getLogger("tsengine.orchestrator")


class Orchestrator:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()

    def detect(self, path: str | Path) -> DetectedFile:
        return _detect.detect_file(path)

    # The "plan": for each detected format, the operation + library chosen. This
    # is the subagent-orchestrator deciding HOW to read each file before it does.
    PLAN = {
        FileKind.EXCEL: ("structured spreadsheet read", "openpyxl"),
        FileKind.CSV: ("delimited parse", "pandas"),
        FileKind.PDF_NATIVE: ("digital text + table extraction", "pdfplumber + PyMuPDF"),
        FileKind.PDF_SCANNED: ("rasterize + OCR, then vision LLM", "PyMuPDF + tesseract + OpenRouter"),
        FileKind.DOCX: ("doc parse, fall back to embedded-image OCR/vision", "python-docx + tesseract/OpenRouter"),
        FileKind.IMAGE: ("OCR + vision LLM", "tesseract + OpenRouter"),
        FileKind.EMAIL: ("parse email body + process attachments", "stdlib email + orchestrator"),
        FileKind.UNKNOWN: ("unsupported", "-"),
    }

    def plan(self, det: DetectedFile) -> dict:
        op, lib = self.PLAN.get(det.kind, ("unknown", "-"))
        return {"kind": det.kind.value, "operation": op, "library": lib,
                "reason": det.reason}

    def extract(self, path: str | Path,
                detected: Optional[DetectedFile] = None) -> RawExtraction:
        det = detected or self.detect(path)
        kind = det.kind
        plan = self.plan(det)
        log.info("plan %s -> %s via %s", Path(path).name, plan["operation"], plan["library"])
        try:
            if kind == FileKind.EXCEL:
                raw = excel.extract(path)
            elif kind == FileKind.CSV:
                raw = csv_ingest.extract(path)
            elif kind == FileKind.PDF_NATIVE:
                raw = pdf_native.extract(path, self.s)
                # native PDF with no extractable text/table -> treat as scanned
                if raw.quality == ExtractionQuality.EMPTY:
                    log.info("native PDF empty, falling back to scanned: %s", path)
                    raw = pdf_scanned.extract(path, self.s)
            elif kind == FileKind.PDF_SCANNED:
                raw = pdf_scanned.extract(path, self.s)
            elif kind == FileKind.DOCX:
                raw = docx_ingest.extract(path, self.s)
            elif kind == FileKind.IMAGE:
                raw = image.extract(path, self.s)
            elif kind == FileKind.EMAIL:
                raw = eml_ingest.extract(path, self.s, self)
            else:
                raw = RawExtraction(file=Path(path).name, kind=FileKind.UNKNOWN,
                                    quality=ExtractionQuality.EMPTY)
                raw.notes.append(f"unsupported file type: {det.ext}")
            raw.meta.setdefault("detected", det.reason)
            raw.meta["plan"] = plan
            return raw
        except Exception as exc:  # never let one bad file kill the run
            log.exception("extraction failed for %s", path)
            raw = RawExtraction(file=Path(path).name, kind=kind,
                                quality=ExtractionQuality.EMPTY)
            raw.notes.append(f"extraction error: {exc}")
            return raw
