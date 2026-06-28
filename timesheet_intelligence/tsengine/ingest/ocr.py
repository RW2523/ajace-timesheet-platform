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


# --------------------------------------------------------------------------- #
# PaddleOCR backend (optional). Much stronger than tesseract on faint / light
# scans. Loaded lazily; absent install -> paddle_available() False -> caller
# transparently falls back to tesseract. Kept OUT of requirements.txt so cloud
# images stay lean; install locally with `pip install paddleocr paddlepaddle`.
# --------------------------------------------------------------------------- #
import threading as _threading

_PADDLE = None
_PADDLE_LOCK = _threading.Lock()
_PADDLE_OK: Optional[bool] = None


def paddle_available() -> bool:
    global _PADDLE_OK
    if _PADDLE_OK is None:
        try:
            import paddleocr  # noqa: F401

            _PADDLE_OK = True
        except Exception:
            _PADDLE_OK = False
    return _PADDLE_OK


def _paddle_engine():
    global _PADDLE
    if _PADDLE is None:
        with _PADDLE_LOCK:
            if _PADDLE is None:
                from paddleocr import PaddleOCR

                _PADDLE = PaddleOCR(lang="en")
    return _PADDLE


def paddle_layout_ocr(path: str | Path, settings: Optional[Settings] = None) -> tuple[str, float]:
    """Layout OCR via PaddleOCR (PP-OCRv6). Reconstructs visual rows from the
    detected boxes. Returns (text, mean_confidence 0..100), ("",0.0) on failure.
    """
    if not paddle_available():
        return "", 0.0
    try:
        res = _paddle_engine().predict(str(path))
    except Exception:
        return "", 0.0
    if not res:
        return "", 0.0
    r0 = res[0]
    get = r0.get if hasattr(r0, "get") else (lambda k, d=None: d)
    texts = get("rec_texts") or []
    scores = get("rec_scores") or []
    boxes = get("rec_boxes")
    if boxes is None or len(boxes) == 0:
        boxes = get("rec_polys") or []

    items = []  # (y_center, x_left, height, text, score0..1)
    for i, t in enumerate(texts):
        if not str(t).strip():
            continue
        box = boxes[i] if i < len(boxes) else None
        if box is None:
            continue
        try:
            arr = list(box)
            if len(arr) == 4 and not hasattr(arr[0], "__len__"):
                x1, y1, x2, y2 = (float(v) for v in arr)
            else:                                 # polygon [[x,y],...]
                xs = [float(p[0]) for p in arr]
                ys = [float(p[1]) for p in arr]
                x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        except Exception:
            continue
        sc = float(scores[i]) if i < len(scores) else 0.0
        items.append(((y1 + y2) / 2, x1, abs(y2 - y1), str(t), sc))
    if not items:
        return "", 0.0

    heights = sorted(it[2] for it in items if it[2] > 0)
    tol = (heights[len(heights) // 2] * 0.6) if heights else 12.0
    items.sort(key=lambda b: b[0])
    rows: list[list] = []
    cur: list = []
    last_y = None
    for yc, x1, _h, txt, sc in items:
        if last_y is None or abs(yc - last_y) <= tol:
            cur.append((x1, txt, sc))
        else:
            rows.append(cur)
            cur = [(x1, txt, sc)]
        last_y = yc
    if cur:
        rows.append(cur)
    lines = ["  ".join(t for _, t, _ in sorted(r, key=lambda z: z[0])) for r in rows]
    allsc = [sc for r in rows for _, _, sc in r]
    mean_conf = round(sum(allsc) / len(allsc) * 100, 1) if allsc else 0.0
    return "\n".join(lines), mean_conf


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
    """Layout-aware OCR dispatcher (the "Layout Finder").

    Returns (reconstructed_text, mean_confidence 0..100) where each visual ROW is
    reconstructed left-to-right so a scanned grid keeps its structure -- this both
    reads more accurately and stops a vision model from hallucinating values into
    blank cells. Backend is chosen by settings.ocr_engine ("auto" prefers
    PaddleOCR when installed -- much stronger on faint/light-text scans -- and
    falls back to tesseract).
    """
    s = settings or get_settings()
    engine = getattr(s, "ocr_engine", "auto")
    if engine == "paddle" and paddle_available():
        txt, conf = paddle_layout_ocr(path, s)
        if txt:                                  # forced paddle, succeeded
            return txt, conf
        # forced paddle failed -> fall through to tesseract
    # tesseract first (fast on every scan)
    txt, conf = _tesseract_layout_ocr(path, s, preprocess)
    # auto: escalate ONLY faint scans (low tesseract confidence) to PaddleOCR --
    # slow on CPU but far more accurate on light text. Good scans keep tesseract's
    # speed, so a full batch stays fast and only the hard files pay the cost.
    if (engine == "auto" and paddle_available()
            and conf < float(getattr(s, "ocr_ground_min_confidence", 55.0))):
        p_txt, p_conf = paddle_layout_ocr(path, s)
        if p_txt and p_conf > conf:
            return p_txt, p_conf
    return txt, conf


def _tesseract_layout_ocr(path: str | Path, settings: Optional[Settings] = None,
                          preprocess: bool = True) -> tuple[str, float]:
    """Tesseract layout OCR: reconstruct rows from word bounding boxes."""
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
