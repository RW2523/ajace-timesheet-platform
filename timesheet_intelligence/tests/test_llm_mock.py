"""LLM path is exercised with a fake router so it runs without any API key."""
import datetime as dt

from tsengine.llm.router import LLMResult
from tsengine.normalize.llm_normalizer import LLMNormalizer
from tsengine.schema import FileKind, RawExtraction, RawImage, SourceRef

CANON = {
    "employee_name": "Jane Doe",
    "employee_id": "E-42",
    "client": "Globex",
    "project": "Apollo",
    "entries": [
        {"date": "2026-04-01", "regular_hours": 8, "overtime_hours": 1,
         "total_hours": 9, "project": "Apollo", "raw": "Apr 1: 9h"},
        {"date": "2026-03-31", "total_hours": 8},   # out of month -> dropped
    ],
    "weekly_totals": [],
    "notes": ["page 2 smudged"],
    "confidence": 0.8,
}


class _FakeClient:
    def vision_message(self, text, paths):
        return {"role": "user", "content": text}


class FakeRouter:
    enabled = True

    def __init__(self, data):
        self._data = data
        self.client = _FakeClient()

    def run(self, task, messages, json_mode=True):
        return LLMResult(ok=True, task=task, model="fake/model", data=self._data)


def test_llm_text_contract_to_entries():
    raw = RawExtraction(file="scan.pdf", kind=FileKind.PDF_SCANNED,
                        text="some ocr text long enough to take the text path " * 5)
    res = LLMNormalizer(FakeRouter(CANON)).normalize(raw, 4, 2026)
    assert res is not None
    assert res.employee_name == "Jane Doe"
    assert res.employee_id == "E-42"
    assert res.client == "Globex"
    dates = {e.date for e in res.entries}
    assert dt.date(2026, 4, 1) in dates
    assert dt.date(2026, 3, 31) not in dates   # month filtering applied
    e = next(e for e in res.entries if e.date == dt.date(2026, 4, 1))
    assert e.regular == 8 and e.overtime == 1 and e.total == 9


def test_llm_vision_path_used_for_imageonly():
    raw = RawExtraction(file="photo.png", kind=FileKind.IMAGE, text="")
    raw.images.append(RawImage(path="/tmp/x.png", source=SourceRef(file="photo.png")))
    res = LLMNormalizer(FakeRouter(CANON)).normalize(raw, 4, 2026)
    assert res is not None
    assert res.quality.value == "vision"
    assert any(e.date == dt.date(2026, 4, 1) for e in res.entries)


def test_llm_disabled_returns_none():
    class Off(FakeRouter):
        enabled = False
    raw = RawExtraction(file="scan.pdf", kind=FileKind.PDF_SCANNED, text="x" * 300)
    assert LLMNormalizer(Off(CANON)).normalize(raw, 4, 2026) is None
