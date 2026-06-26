"""Local OCR + rasterization utilities.

OCR is done through the system ``tesseract`` binary via subprocess (no extra
python binding required). PDF pages are rasterized with PyMuPDF (no poppler
dependency). Light image preprocessing (grayscale + autocontrast + upscaling of
small images) improves OCR on faint scans.

These are *fallbacks*: when an OpenRouter vision model is configured the
orchestrator prefers it, but local OCR keeps the engine fully functional with
zero API access.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..settings import Settings, get_settings


def tesseract_available(settings: Optional[Settings] = None) -> bool:
    s = settings or get_settings()
    return shutil.which(s.tesseract_cmd) is not None


def _preprocess(img):
    """Return a cleaned PIL image better suited to OCR."""
    from PIL import Image, ImageOps

    if img.mode != "L":
        img = img.convert("L")
    img = ImageOps.autocontrast(img)
    # upscale small images so glyphs are tall enough for tesseract
    w, h = img.size
    if max(w, h) < 1600:
        scale = 1600 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def ocr_image(path: str | Path, settings: Optional[Settings] = None,
              preprocess: bool = True) -> str:
    s = settings or get_settings()
    if not tesseract_available(s):
        return ""
    src = Path(path)
    tmp_png: Optional[str] = None
    target = str(src)
    if preprocess:
        try:
            from PIL import Image

            img = _preprocess(Image.open(src))
            fd, tmp_png = tempfile.mkstemp(suffix=".png")  # secure, no TOCTOU race
            os.close(fd)
            img.save(tmp_png)
            target = tmp_png
        except Exception:
            target = str(src)
    try:
        out = subprocess.run(
            [s.tesseract_cmd, target, "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=120,
        )
        return out.stdout or ""
    except Exception:
        return ""
    finally:
        if tmp_png:
            try:
                Path(tmp_png).unlink(missing_ok=True)
            except Exception:
                pass


def layout_ocr(path: str | Path, settings: Optional[Settings] = None,
               preprocess: bool = True) -> tuple[str, float]:
    """Layout-aware OCR (the "Layout Finder").

    Unlike plain OCR (which flattens a table into a column-less stream), this
    uses tesseract's word bounding boxes to reconstruct each visual ROW with its
    words in left-to-right order. A scanned grid therefore keeps its structure
    (``Sat | 0`` stays a row), which both reads far more accurately and stops a
    vision model from *hallucinating* values into blank cells. Returns
    (reconstructed_text, mean_word_confidence 0..100).
    """
    import csv as _csv
    from collections import defaultdict

    s = settings or get_settings()
    if not tesseract_available(s):
        return "", 0.0
    src = Path(path)
    tmp_png: Optional[str] = None
    target = str(src)
    if preprocess:
        try:
            from PIL import Image

            img = _preprocess(Image.open(src))
            fd, tmp_png = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img.save(tmp_png)
            target = tmp_png
        except Exception:
            target = str(src)
    try:
        out = subprocess.run(
            [s.tesseract_cmd, target, "stdout", "--psm", "6", "tsv"],
            capture_output=True, text=True, timeout=120,
        ).stdout or ""
    except Exception:
        return "", 0.0
    finally:
        if tmp_png:
            try:
                Path(tmp_png).unlink(missing_ok=True)
            except Exception:
                pass

    words = [r for r in _csv.DictReader(out.splitlines(), delimiter="\t")
             if r.get("text", "").strip() and r.get("conf", "-1") not in ("-1", "")]
    if not words:
        return "", 0.0
    lines: dict = defaultdict(list)
    confs: list[float] = []
    for w in words:
        try:
            key = (int(w["block_num"]), int(w["par_num"]), int(w["line_num"]))
            lines[key].append((int(w["left"]), w["text"]))
            c = float(w["conf"])
            if c >= 0:
                confs.append(c)
        except (ValueError, KeyError):
            continue
    recon = "\n".join(" ".join(t for _, t in sorted(ws)) for _, ws in sorted(lines.items()))
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    return recon, round(mean_conf, 1)


def render_pdf_pages(path: str | Path, out_dir: str | Path,
                     dpi: Optional[int] = None,
                     settings: Optional[Settings] = None) -> list[tuple[int, str]]:
    """Rasterize every PDF page to PNG. Returns [(page_number_1based, png_path)]."""
    import fitz

    s = settings or get_settings()
    dpi = dpi or s.ocr_dpi
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    cap = max(1, int(getattr(s, "max_pdf_pages", 60)))
    results: list[tuple[int, str]] = []
    with fitz.open(str(path)) as doc:
        for i, pg in enumerate(doc, start=1):
            if i > cap:                       # DoS guard against huge PDFs
                break
            pix = pg.get_pixmap(dpi=dpi)
            png = out_dir / f"{stem}_p{i}.png"
            pix.save(str(png))
            results.append((i, str(png)))
    return results
