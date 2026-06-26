"""Email (.eml) extractor.

Timesheets are often forwarded as email: the hours may sit in the body (a pasted
weekly table) AND/OR in attachments (a screenshot, PDF, or workbook). We pull the
body text and run every attachment back through the orchestrator, merging all of
it into one RawExtraction so the normalizer/LLM sees the whole picture.
"""
from __future__ import annotations

import email
import email.policy
import re
import tempfile
from pathlib import Path
from typing import Optional

from ..schema import ExtractionQuality, FileKind, RawExtraction
from ..settings import Settings, get_settings


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"[ \t]+", " ", text)


def extract(path: str | Path, settings: Optional[Settings] = None,
            orchestrator=None) -> RawExtraction:
    s = settings or get_settings()
    p = Path(path)
    raw = RawExtraction(file=p.name, kind=FileKind.EMAIL,
                        quality=ExtractionQuality.EMPTY)
    try:
        msg = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
    except Exception as exc:
        raw.notes.append(f"eml parse failed: {exc}")
        return raw

    header = [f"{h.title()}: {msg.get(h)}" for h in ("subject", "from", "date")
              if msg.get(h)]

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attach_texts: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fname = part.get_filename()
        disp = part.get_content_disposition()
        is_attachment = disp == "attachment" or (
            fname and not ctype.startswith("text/"))
        if is_attachment:
            safe = Path(fname or "attachment").name
            if safe.lower().endswith(".eml") or not orchestrator:
                continue                       # don't recurse into nested emails
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            tmp = Path(tempfile.mkdtemp(prefix="ts_eml_")) / safe
            try:
                tmp.write_bytes(payload)
                sub = orchestrator.extract(tmp)
            except Exception as exc:
                raw.notes.append(f"attachment {safe} failed: {exc}")
                continue
            raw.tables.extend(sub.tables)
            raw.images.extend(sub.images)
            if sub.text and sub.text.strip():
                attach_texts.append(f"[attachment: {safe}]\n{sub.text.strip()}")
        else:
            try:
                content = part.get_content()
            except Exception:
                content = ""
            if ctype == "text/plain":
                plain_parts.append(content)
            elif ctype == "text/html":
                html_parts.append(_strip_html(content))

    body = "\n".join(plain_parts).strip() or "\n".join(html_parts).strip()
    pieces = []
    if header:
        pieces.append("\n".join(header))
    if body:
        pieces.append(body)
    pieces.extend(attach_texts)
    raw.text = "\n\n".join(pieces).strip()

    if raw.text and len(raw.text) > 40:
        raw.quality = ExtractionQuality.NATIVE
    elif raw.images:
        raw.quality = ExtractionQuality.VISION
    raw.meta["email"] = {
        "subject": msg.get("subject"), "from": msg.get("from"),
        "attachments": len(raw.images) + len(raw.tables),
    }
    return raw
