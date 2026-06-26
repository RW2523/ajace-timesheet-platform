"""Inspect a file and decide its format + quality so the orchestrator can route
it to the cheapest sufficient extractor.

The key non-trivial call is *native-text PDF vs scanned PDF*: we open the PDF,
measure how much real text it carries per page, and fall back to OCR/vision when
it is effectively an image.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..schema import FileKind

EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
CSV_EXT = {".csv", ".tsv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
DOCX_EXT = {".docx"}
PDF_EXT = {".pdf"}
EMAIL_EXT = {".eml"}

# A native PDF page typically carries far more than this many characters.
NATIVE_PDF_CHARS_PER_PAGE = 80


@dataclass
class DetectedFile:
    path: str
    kind: FileKind
    ext: str
    size: int
    pages: int = 0
    native_text_chars: int = 0
    needs_ocr: bool = False
    reason: str = ""
    meta: dict = field(default_factory=dict)


def _pdf_text_profile(path: str) -> tuple[int, int, float]:
    """Return (page_count, total_text_chars, printable_ratio) using PyMuPDF.

    printable_ratio guards against PDFs whose embedded text is garbage (Type3 /
    unmapped fonts return control bytes): lots of "chars" but not real text.
    """
    import fitz  # PyMuPDF

    chars = 0
    printable = 0
    with fitz.open(path) as doc:
        npages = len(doc)
        for pg in doc:
            t = pg.get_text().strip()
            chars += len(t)
            printable += sum(1 for c in t if c.isalnum() or c.isspace()
                             or c in ".,:/-$%()")
    ratio = (printable / chars) if chars else 0.0
    return npages, chars, ratio


def _sniff_kind(path: Path) -> Optional[str]:
    """Identify a file by its CONTENT (magic bytes), independent of extension.

    Robustness for missing/wrong extensions: a ZIP that contains ``word/`` is a
    DOCX, one containing ``xl/`` is an XLSX, ``%PDF`` is a PDF, etc.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except Exception:
        return None
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head[:4] == b"\x89PNG" or head[:3] == b"\xff\xd8\xff" or head[:6] in (b"GIF87a", b"GIF89a"):
        return ".png"
    if head[:4] == b"PK\x03\x04":          # zip container: xlsx vs docx
        try:
            import zipfile
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if any(n.startswith("word/") for n in names):
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
        except Exception:
            return None
    return None


def detect_file(path: str | Path) -> DetectedFile:
    p = Path(path)
    ext = p.suffix.lower()
    size = p.stat().st_size if p.exists() else 0

    # if the extension is unknown/misleading, identify by content
    known = EXCEL_EXT | CSV_EXT | IMAGE_EXT | DOCX_EXT | PDF_EXT | EMAIL_EXT
    if ext not in known:
        sniffed = _sniff_kind(p)
        if sniffed:
            ext = sniffed

    if ext in EMAIL_EXT:
        return DetectedFile(str(p), FileKind.EMAIL, ext, size,
                            reason="email (.eml) -> body + attachments")
    if ext in EXCEL_EXT:
        return DetectedFile(str(p), FileKind.EXCEL, ext, size, reason="spreadsheet")
    if ext in CSV_EXT:
        return DetectedFile(str(p), FileKind.CSV, ext, size, reason="delimited text")
    if ext in DOCX_EXT:
        return DetectedFile(str(p), FileKind.DOCX, ext, size, reason="word document")
    if ext in IMAGE_EXT:
        return DetectedFile(str(p), FileKind.IMAGE, ext, size, needs_ocr=True,
                            reason="raster image -> OCR/vision")
    if ext in PDF_EXT:
        try:
            pages, chars, printable_ratio = _pdf_text_profile(str(p))
        except Exception as exc:  # corrupt / unreadable
            return DetectedFile(str(p), FileKind.PDF_SCANNED, ext, size, needs_ocr=True,
                                reason=f"pdf profile failed ({exc}); assume scanned")
        per_page = chars / max(pages, 1)
        # garbage text layer (unmapped/Type3 fonts) -> treat as scanned -> OCR/vision
        if chars > 0 and printable_ratio < 0.6:
            return DetectedFile(str(p), FileKind.PDF_SCANNED, ext, size, pages=pages,
                                native_text_chars=chars, needs_ocr=True,
                                reason=f"unreadable text layer ({printable_ratio:.0%} printable); OCR")
        if per_page >= NATIVE_PDF_CHARS_PER_PAGE:
            return DetectedFile(str(p), FileKind.PDF_NATIVE, ext, size, pages=pages,
                                native_text_chars=chars,
                                reason=f"native text ({per_page:.0f} chars/page)")
        return DetectedFile(str(p), FileKind.PDF_SCANNED, ext, size, pages=pages,
                            native_text_chars=chars, needs_ocr=True,
                            reason=f"scanned/image pdf ({per_page:.0f} chars/page)")

    return DetectedFile(str(p), FileKind.UNKNOWN, ext, size, reason="unknown extension")
