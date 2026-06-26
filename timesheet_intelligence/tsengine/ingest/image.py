"""Image extractor (PNG/JPG/...). Local OCR for text, keeps the image for vision."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schema import (ExtractionQuality, FileKind, RawExtraction, RawImage,
                      SourceRef)
from ..settings import Settings, get_settings
from .excel import _name_hint
from .ocr import layout_ocr, tesseract_available


def extract(path: str | Path, settings: Optional[Settings] = None) -> RawExtraction:
    s = settings or get_settings()
    p = Path(path)
    rel = p.name
    raw = RawExtraction(file=rel, kind=FileKind.IMAGE, quality=ExtractionQuality.OCR)
    raw.meta["name_hint"] = _name_hint(p.stem)

    try:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
    except Exception as exc:
        raw.notes.append(f"cannot open image: {exc}")
        raw.quality = ExtractionQuality.EMPTY
        return raw

    raw.images.append(RawImage(
        path=str(p), width=w, height=h,
        source=SourceRef(file=rel, page=1, region="full image", extractor="image"),
    ))
    raw.sources.append(SourceRef(file=rel, page=1, extractor="image"))

    if tesseract_available(s) and s.use_local_ocr:
        txt, conf = layout_ocr(p, s)        # layout-aware: keeps grid structure
        raw.text = txt.strip()
        raw.meta["ocr_confidence"] = conf
    if not raw.text:
        raw.notes.append("no local OCR text; vision model recommended")
    return raw
