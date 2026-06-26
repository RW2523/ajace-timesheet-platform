"""CSV/TSV extractor (pandas with a csv fallback). One RawTable with headers."""
from __future__ import annotations

from pathlib import Path

from ..schema import (ExtractionQuality, FileKind, RawExtraction, RawTable,
                      SourceRef)
from .excel import _name_hint


def extract(path: str | Path) -> RawExtraction:
    p = Path(path)
    rel = p.name
    raw = RawExtraction(file=rel, kind=FileKind.CSV, quality=ExtractionQuality.NATIVE)
    sep = "\t" if p.suffix.lower() == ".tsv" else ","

    headers: list[str] = []
    rows: list[list] = []
    try:
        import pandas as pd

        df = pd.read_csv(p, sep=sep, dtype=str, keep_default_na=False)
        headers = [str(c) for c in df.columns]
        rows = df.values.tolist()
    except Exception:
        import csv as _csv

        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            reader = list(_csv.reader(fh, delimiter=sep))
        if reader:
            headers = [str(c) for c in reader[0]]
            rows = reader[1:]

    if not headers and not rows:
        raw.quality = ExtractionQuality.EMPTY
        raw.notes.append("empty csv")
        return raw

    raw.tables.append(RawTable(
        headers=headers, rows=rows, title=rel,
        source=SourceRef(file=rel, extractor="csv"),
    ))
    raw.sources.append(SourceRef(file=rel, extractor="csv"))
    lines = [" | ".join(headers)]
    for i, r in enumerate(rows[:200], start=2):
        lines.append(f"[r{i}] " + " | ".join("" if c is None else str(c) for c in r))
    raw.text = "\n".join(lines)
    raw.meta["name_hint"] = _name_hint(p.stem)
    return raw
