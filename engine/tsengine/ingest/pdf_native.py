"""Native (digital-text) PDF extractor.

Uses pdfplumber for ruled-table extraction and word-positioned text, with
PyMuPDF as a text fallback. Tables come out per page with page-level source
references. Many timesheet PDFs are *not* nicely ruled (text is positioned
free-form), so we always keep the full text too for the normalizer/LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schema import (ExtractionQuality, FileKind, RawExtraction, RawImage,
                      RawTable, SourceRef)
from ..settings import Settings, get_settings
from .excel import _name_hint


def extract(path: str | Path, settings: Optional[Settings] = None) -> RawExtraction:
    s = settings or get_settings()
    p = Path(path)
    rel = p.name
    raw = RawExtraction(file=rel, kind=FileKind.PDF_NATIVE, quality=ExtractionQuality.NATIVE)

    text_parts: list[str] = []
    table_settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}

    try:
        import pdfplumber

        with pdfplumber.open(str(p)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                ptext = page.extract_text() or ""
                if ptext.strip():
                    text_parts.append(f"\n----- page {pno} -----\n{ptext}")
                # ruled tables first, then a looser text strategy
                tables = []
                try:
                    tables = page.extract_tables(table_settings) or []
                    if not tables:
                        tables = page.extract_tables() or []
                except Exception:
                    tables = []
                for ti, tbl in enumerate(tables):
                    cleaned = [[(c or "").strip() if isinstance(c, str) else c
                                for c in row] for row in tbl if row]
                    if not cleaned or len(cleaned) < 2:
                        continue
                    headers = [str(c) if c is not None else "" for c in cleaned[0]]
                    raw.tables.append(RawTable(
                        headers=headers,
                        rows=cleaned[1:],
                        title=f"page {pno} table {ti + 1}",
                        source=SourceRef(file=rel, page=pno, extractor="pdf_native"),
                    ))
                raw.sources.append(SourceRef(file=rel, page=pno, extractor="pdf_native"))
    except Exception as exc:
        raw.notes.append(f"pdfplumber failed: {exc}")

    # Docling's TableFormer recovers grids pdfplumber's line-ruling misses
    # (borderless / merged timesheets). We only fall back to it when pdfplumber
    # found NO tables: A/B testing showed that overriding pdfplumber's *working*
    # tables can regress files where Docling over-segments the grid, whereas
    # filling the empty cases is a strict win (resolves some PDFs without the LLM).
    if s.use_docling and not raw.tables:
        try:
            from .docling_ingest import docling_tables

            dtables, dmd = docling_tables(p, rel)
            if dtables:
                raw.tables = dtables
                raw.notes.append(f"docling: {len(dtables)} table(s) (pdfplumber found none)")
                if not text_parts and dmd:
                    text_parts.append(dmd)
        except Exception as exc:
            raw.notes.append(f"docling skipped: {exc}")

    full_text = "\n".join(text_parts).strip()
    if not full_text:
        # fall back to PyMuPDF text
        try:
            import fitz

            with fitz.open(str(p)) as doc:
                full_text = "\n".join(pg.get_text() for pg in doc).strip()
        except Exception as exc:
            raw.notes.append(f"fitz text failed: {exc}")

    raw.text = full_text
    raw.meta["name_hint"] = _name_hint(p.stem)

    # Some "native" PDFs put the actual timesheet grid in an embedded raster
    # image on a page with little/no text (e.g. an emailed screenshot). Render
    # those pages so the LLM vision fallback can read them.
    try:
        import fitz

        img_dir = s.output_path / "_evidence" / p.stem
        with fitz.open(str(p)) as doc:
            for pno, page in enumerate(doc, start=1):
                txt = (page.get_text() or "").strip()
                if len(txt) < 60 and page.get_images():
                    img_dir.mkdir(parents=True, exist_ok=True)
                    pix = page.get_pixmap(dpi=s.ocr_dpi)
                    png = img_dir / f"{p.stem}_p{pno}.png"
                    pix.save(str(png))
                    raw.images.append(RawImage(
                        path=str(png), width=pix.width, height=pix.height,
                        source=SourceRef(file=rel, page=pno, region=f"page {pno} image",
                                         extractor="pdf_native_render")))
        if raw.images:
            raw.notes.append(f"{len(raw.images)} image-only page(s) rendered for vision")
    except Exception as exc:
        raw.notes.append(f"page-image render failed: {exc}")

    if not full_text and not raw.tables and not raw.images:
        raw.quality = ExtractionQuality.EMPTY
    return raw
