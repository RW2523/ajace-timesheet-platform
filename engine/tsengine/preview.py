"""Convert any source file to a previewable PDF.

The UI previews every timesheet as a PDF (browsers render PDF natively and
scroll it for free). This module normalizes the zoo of input formats:

    .pdf            -> served as-is
    image (png/jpg) -> wrapped into a one-page PDF (Pillow)
    .xlsx/.csv/.docx-> rendered to PDF via LibreOffice headless (high fidelity)

Converted PDFs are cached under ``output/_pdf/`` and only rebuilt when the
source is newer.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .settings import Settings, get_settings

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
OFFICE_EXT = {".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".docx", ".doc", ".odt", ".ods"}

_SOFFICE_CANDIDATES = [
    "soffice", "libreoffice",
    "/opt/homebrew/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice", "/usr/bin/libreoffice",
]


def _find_soffice() -> Optional[str]:
    for c in _SOFFICE_CANDIDATES:
        p = shutil.which(c) if "/" not in c else (c if Path(c).exists() else None)
        if p:
            return p
    return None


def _pdf_cache_dir(settings: Settings) -> Path:
    d = settings.output_path / "_pdf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fresh(out: Path, src: Path) -> bool:
    return out.exists() and out.stat().st_size > 0 and out.stat().st_mtime >= src.stat().st_mtime


def to_pdf(src: str | Path, settings: Optional[Settings] = None) -> Optional[str]:
    """Return a path to a PDF rendition of ``src`` (cached). None if unconvertible."""
    s = settings or get_settings()
    src = Path(src)
    if not src.exists():
        return None
    out_dir = _pdf_cache_dir(s)
    ext = src.suffix.lower()
    out = out_dir / (src.stem + ".pdf")

    if _fresh(out, src):
        return str(out)

    try:
        if ext == ".pdf":
            shutil.copy2(src, out)
            return str(out)
        if ext in IMAGE_EXT:
            from PIL import Image

            im = Image.open(src)
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            im.save(out, "PDF", resolution=150.0)
            return str(out)
        if ext in OFFICE_EXT:
            soffice = _find_soffice()
            if not soffice:
                return None
            # LibreOffice writes <stem>.pdf into outdir
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir",
                 str(out_dir), str(src)],
                capture_output=True, timeout=180,
                env={"HOME": str(out_dir), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
            )
            produced = out_dir / (src.stem + ".pdf")
            return str(produced) if produced.exists() and produced.stat().st_size > 0 else None
    except Exception:
        return None
    return None


def pdf_page_images(src: str | Path, settings: Optional[Settings] = None,
                    dpi: int = 120) -> list[str]:
    """Render the previewable PDF of ``src`` to per-page PNGs (cached) so the UI
    can show a scrollable image strip -- renders in every browser, no PDF plugin
    needed. Returns ordered page-image paths."""
    s = settings or get_settings()
    pdf = to_pdf(src, s)
    if not pdf:
        return []
    src = Path(src)
    img_dir = s.output_path / "_pdf_img" / src.stem
    img_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(img_dir.glob("p*.png"), key=lambda p: int(p.stem[1:] or 0))
    if existing and existing[0].stat().st_mtime >= Path(pdf).stat().st_mtime:
        return [str(p) for p in existing]
    for old in existing:
        old.unlink(missing_ok=True)
    out: list[str] = []
    try:
        import fitz

        with fitz.open(pdf) as doc:
            for i, page in enumerate(doc, start=1):
                if i > 40:                       # cap pages for the preview
                    break
                png = img_dir / f"p{i}.png"
                page.get_pixmap(dpi=dpi).save(str(png))
                out.append(str(png))
    except Exception:
        return []
    return out


def batch_convert(files: list[str | Path], settings: Optional[Settings] = None) -> dict[str, str]:
    """Convert many files; returns {source_path: pdf_path} for successes.

    Office files are converted in a single LibreOffice invocation (one startup,
    much faster); images/PDFs are handled individually.
    """
    s = settings or get_settings()
    out_dir = _pdf_cache_dir(s)
    result: dict[str, str] = {}

    office_batch: list[Path] = []
    for f in files:
        p = Path(f)
        if not p.exists():
            continue
        ext = p.suffix.lower()
        if ext in OFFICE_EXT and not _fresh(out_dir / (p.stem + ".pdf"), p):
            office_batch.append(p)
        else:
            pdf = to_pdf(p, s)            # pdf/image, or already-cached office
            if pdf:
                result[str(p)] = pdf

    if office_batch:
        soffice = _find_soffice()
        if soffice:
            try:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "pdf", "--outdir",
                     str(out_dir), *[str(p) for p in office_batch]],
                    capture_output=True, timeout=600,
                    env={"HOME": str(out_dir), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
                )
            except Exception:
                pass
        for p in office_batch:
            produced = out_dir / (p.stem + ".pdf")
            if produced.exists() and produced.stat().st_size > 0:
                result[str(p)] = str(produced)
    return result
