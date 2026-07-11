"""Docling (IBM) PDF table extraction — TableFormer table-structure recognition.

Produces high-accuracy structured tables from PDFs, especially the borderless /
merged-cell timesheet grids where pdfplumber's line-ruling heuristics fail
(TableFormer TEDS-Struct ~96.75% on PubTabNet vs pdfplumber's no-ML approach).

Design:
  * loaded lazily — the converter (and its models) only spin up the first time a
    PDF actually needs it, so importing the engine stays cheap;
  * fails soft — if docling isn't installed or a conversion errors, callers get
    ([], "") back and fall through to the existing pdfplumber path;
  * thread-safe singleton — one converter shared across the parallel ingest.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from ..schema import RawTable, SourceRef

_CONVERTER = None
_LOCK = threading.Lock()
_AVAILABLE: Optional[bool] = None


def available() -> bool:
    """True if docling can be imported (cached)."""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            import docling  # noqa: F401

            _AVAILABLE = True
        except Exception:
            _AVAILABLE = False
    return _AVAILABLE


def _converter():
    global _CONVERTER
    if _CONVERTER is None:
        with _LOCK:
            if _CONVERTER is None:
                from docling.document_converter import DocumentConverter

                _CONVERTER = DocumentConverter()
    return _CONVERTER


def docling_tables(path: str | Path, rel: Optional[str] = None) -> tuple[list[RawTable], str]:
    """Return (tables, markdown_text) from Docling, or ([], "") on any failure."""
    if not available():
        return [], ""
    p = Path(path)
    rel = rel or p.name
    try:
        doc = _converter().convert(str(p)).document
    except Exception:
        return [], ""

    out: list[RawTable] = []
    for ti, tb in enumerate(getattr(doc, "tables", []) or []):
        df = None
        try:
            df = tb.export_to_dataframe(doc=doc)     # newer docling wants doc
        except TypeError:
            try:
                df = tb.export_to_dataframe()
            except Exception:
                df = None
        except Exception:
            df = None
        if df is None or df.shape[0] < 1 or df.shape[1] < 2:
            continue
        headers = [str(c) for c in df.columns]
        rows = [["" if v is None else str(v) for v in row] for row in df.values.tolist()]
        page = None
        try:
            page = tb.prov[0].page_no
        except Exception:
            pass
        out.append(RawTable(
            headers=headers, rows=rows, title=f"docling table {ti + 1}",
            source=SourceRef(file=rel, page=page, extractor="docling"),
        ))

    md = ""
    try:
        md = doc.export_to_markdown()
    except Exception:
        md = ""
    return out, md
