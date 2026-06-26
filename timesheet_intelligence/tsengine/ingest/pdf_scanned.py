"""Scanned / image-only PDF extractor.

Rasterizes each page to PNG (PyMuPDF), runs local tesseract OCR to populate
text, and records the page images so a vision model can be used downstream when
configured. Output ``quality`` is OCR; the orchestrator may upgrade to VISION.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schema import (ExtractionQuality, FileKind, RawExtraction, RawImage,
                      SourceRef)
from ..settings import Settings, get_settings
from .excel import _name_hint
from .ocr import layout_ocr, render_pdf_pages, tesseract_available


def extract(path: str | Path, settings: Optional[Settings] = None) -> RawExtraction:
    s = settings or get_settings()
    p = Path(path)
    rel = p.name
    raw = RawExtraction(file=rel, kind=FileKind.PDF_SCANNED, quality=ExtractionQuality.OCR)
    raw.meta["name_hint"] = _name_hint(p.stem)

    img_dir = s.output_path / "_evidence" / p.stem
    try:
        pages = render_pdf_pages(p, img_dir, settings=s)
    except Exception as exc:
        raw.notes.append(f"rasterization failed: {exc}")
        raw.quality = ExtractionQuality.EMPTY
        return raw

    text_parts: list[str] = []
    confs: list[float] = []
    have_ocr = tesseract_available(s) and s.use_local_ocr
    for pno, png in pages:
        from PIL import Image

        try:
            with Image.open(png) as im:
                w, h = im.size
        except Exception:
            w = h = None
        raw.images.append(RawImage(
            path=png, width=w, height=h,
            source=SourceRef(file=rel, page=pno, region=f"page {pno}", extractor="pdf_scanned"),
        ))
        raw.sources.append(SourceRef(file=rel, page=pno, extractor="pdf_scanned"))
        if have_ocr:
            # layout-aware OCR keeps the grid structure (rows/columns), which both
            # reads more accurately and stops a vision model later from inventing
            # values into blank cells.
            txt, conf = layout_ocr(png, s)
            if txt.strip():
                text_parts.append(f"\n----- page {pno} (OCR) -----\n{txt}")
                confs.append(conf)

    raw.text = "\n".join(text_parts).strip()
    raw.meta["ocr_confidence"] = round(sum(confs) / len(confs), 1) if confs else 0.0
    if not raw.text:
        raw.notes.append("no local OCR text; vision model recommended")
        if not have_ocr:
            raw.quality = ExtractionQuality.EMPTY if not raw.images else ExtractionQuality.OCR
    raw.meta["page_count"] = len(pages)
    return raw
