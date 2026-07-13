"""Direct track (flow='direct') exercised with a mock model client -- no API key.

Covers: file->model contract mapping, the escalation gate, cross-model
disagreement, month-level sanity checks, and human-review routing.
"""
import datetime as dt
import threading

from tsengine.direct.extractor import DirectExtractor
from tsengine.llm.client import ChatResponse
from tsengine.schema import EmployeeMonth, FileKind, Issue, IssueCode, IssueSeverity
from tsengine.settings import Settings
from tsengine.validate.validator import Validator


def _mega(total_days=17, conf=0.9, matches=True, discs=None, is_ts=True):
    entries = [{"date": f"2026-04-{d:02d}", "regular_hours": 8, "overtime_hours": 0,
                "total_hours": 8} for d in range(1, total_days + 1)]
    return {
        "document_type": "timesheet" if is_ts else "invoice",
        "is_timesheet": is_ts,
        "employee_name": "Elangovan Krishnaswamy", "client": "HCPSS",
        "entries": entries, "weekly_totals": [],
        "stated_total": total_days * 8.0, "confidence": conf,
        "self_check": {"sum_of_daily_totals": total_days * 8.0,
                       "matches_stated_total": matches,
                       "per_day_reg_ot_consistent": True,
                       "discrepancies": discs or []},
        "ambiguities": [], "handwritten_or_faint": False,
    }


class _FakeClient:
    """Returns a scripted mega-contract per model id."""
    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    def file_message(self, text, file_path=None, images=None):
        return {"role": "user", "content": text}

    def chat(self, model, messages, **kw):
        self.calls.append(model)
        import json
        data = self.by_model.get(model, self.by_model.get("*"))
        return ChatResponse(text=json.dumps(data), model=model, raw={},
                            usage={"total_tokens": 100, "cost": 0.001})


class _FakeRouter:
    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._dead = set()
        self.calls = 0

    def _record(self, model, usage):
        pass


def _extractor(by_model, **over):
    # ladder tests assert exact call sequences; the blind verify pass adds a call,
    # so it is OFF here unless a test opts in (the verify tests below set it True).
    over.setdefault("direct_verify", False)
    s = Settings(flow="direct", **over)
    return DirectExtractor(_FakeRouter(_FakeClient(by_model)), s), s


def test_direct_maps_contract_and_stops_at_primary():
    ext, s = _extractor({"*": _mega(conf=0.9)})
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res and len(res) == 1
    r = res[0]
    assert r.employee_name == "Elangovan Krishnaswamy"
    assert sum(e.total for e in r.entries) == 136.0
    assert r.method == "direct:openai/gpt-5.4-nano"          # never escalated
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]


def test_direct_escalates_on_low_confidence():
    ext, s = _extractor({
        "openai/gpt-5.4-nano": _mega(conf=0.4),
        "openai/gpt-5.4-mini": _mega(conf=0.92),
    })
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res[0].method == "direct:openai/gpt-5.4-mini"     # climbed one rung
    assert ext.router.client.calls[:2] == ["openai/gpt-5.4-nano", "openai/gpt-5.4-mini"]


def test_direct_self_check_mismatch_routes_to_review_not_escalation():
    # confident but the doc's stated total != daily sum -> keep primary, note it
    # (validator's TOTAL_MISMATCH warning routes to needs_review); do NOT escalate
    # and do NOT block on the self-check alone.
    ext, s = _extractor({"*": _mega(conf=0.9, matches=False, discs=["stated 168 != sum 176"])})
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # did NOT escalate
    assert res[0].needs_llm is False                           # self-check alone != block
    assert any("self-check" in n for n in res[0].notes)        # but it IS surfaced


def test_direct_rejects_non_timesheet():
    ext, s = _extractor({"*": _mega(is_ts=False)})
    res = ext.extract("invoice.pdf", "invoice.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res is None


def test_direct_flags_cross_model_disagreement():
    ext, s = _extractor({
        "openai/gpt-5.4-nano": _mega(total_days=17, conf=0.5),   # 136h, escalates
        "openai/gpt-5.4-mini": _mega(total_days=20, conf=0.5),   # 160h, escalates
        "openai/gpt-5": _mega(total_days=22, conf=0.5),          # 176h
    })
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res[0].needs_llm is True
    assert any("disagreed" in n for n in res[0].notes)


# -- validator: month checks + review routing --------------------------------
def _em(total, days, conf, ot=0.0):
    return EmployeeMonth(month=4, year=2026, monthly_total=total, monthly_regular=total - ot,
                         monthly_overtime=ot, days_worked=days, confidence=conf)


def test_month_check_flags_impossible_daycount():
    em = _em(300, 25, 0.9)                    # April 2026 has 22 weekdays
    Validator().validate(em)
    assert any(i.code == IssueCode.INVALID and i.severity == IssueSeverity.ERROR
               for i in em.issues)
    assert em.review_status == "blocked"


def test_month_check_flags_high_total():
    em = _em(240, 20, 0.9)
    Validator().validate(em)
    assert any("unusually high" in i.message for i in em.issues)


def test_review_routing_autoaccept():
    em = _em(136, 17, 0.95)
    Validator().validate(em)
    assert em.review_status == "auto_accepted"


def test_review_routing_needs_review_on_medium_confidence():
    em = _em(136, 17, 0.7)                    # below autoaccept 0.85, no errors
    Validator().validate(em)
    assert em.review_status == "needs_review"


# --- step 4: consensus prompt clauses + blindness guard ------------------- #
def test_direct_prompt_carries_consensus_clauses():
    from tsengine.llm.prompts import direct_extract_system
    p = direct_extract_system(5, 2026)
    # month-clip, portal no-double-count, printed-total/excluded-row, approver
    assert "MONTH BOUNDARY" in p
    assert "NEVER DOUBLE-COUNT A PERIOD" in p
    assert "PRINTED TOTAL vs EXCLUDED ROWS" in p
    assert "APPROVER" in p and "manager_name" in p
    # the boundary clause must steer a crossing period total away from stated_total
    assert "stated_total null" in p or "leave stated_total null" in p


def test_direct_read_is_blind_of_any_candidate():
    # BLINDNESS: the model is never shown a pre-computed/deterministic total, so
    # its read cannot be biased by one. The per-file instruction is a fixed string
    # with no numbers, and the system prompt is a pure function of (month, year).
    captured = {}

    class _Rec(_FakeClient):
        def chat(self, model, messages, **kw):
            captured["messages"] = messages
            return super().chat(model, messages, **kw)

    ext = DirectExtractor(_FakeRouter(_Rec({"*": _mega(conf=0.9)})),
                          Settings(flow="direct", direct_verify=False))
    ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    msgs = captured["messages"]
    user = [m for m in msgs if m["role"] == "user"][0]
    assert user["content"] == "Extract the full mega-contract JSON for this document now."
    assert "candidate" not in str(msgs).lower()
    # the system prompt depends only on month/year, not on any prior read
    from tsengine.llm.prompts import direct_extract_system
    assert msgs[0]["content"] == direct_extract_system(4, 2026)


# --- advanced direct: blind self-verification pass ------------------------ #
def test_direct_verify_confirms_on_agreement():
    # primary reads 136h (17 days); a blind re-read agrees -> confirmed + boosted
    ext, s = _extractor(
        {"openai/gpt-5.4-nano": _mega(total_days=17, conf=0.8),
         "verify-model": {"monthly_total": 136, "days_worked": 17, "confidence": 0.9}},
        direct_verify=True, direct_verify_mode="always", direct_verify_model="verify-model")
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.verification == "confirmed"
    assert r.confidence >= 0.9
    assert "verify-model" in ext.router.client.calls          # the extra cheap call ran


def test_direct_verify_flags_on_disagreement():
    # primary reads 136h but a blind re-read says 96h -> flagged for review
    ext, s = _extractor(
        {"openai/gpt-5.4-nano": _mega(total_days=17, conf=0.9),
         "verify-model": {"monthly_total": 96, "days_worked": 12, "confidence": 0.8}},
        direct_verify=True, direct_verify_mode="always", direct_verify_model="verify-model")
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.verification != "confirmed"
    assert r.confidence <= 0.6                                 # -> needs_review
    assert any("DISAGREED" in n for n in r.notes)


def test_direct_verify_off_makes_no_extra_call():
    ext, s = _extractor({"openai/gpt-5.4-nano": _mega(conf=0.9)}, direct_verify=False)
    ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # no verify call


# --- Direct++ phases: dedupe / repair / gate / budget / scheduling --------- #
def test_dedupe_repeated_dates_counted_once():
    # the same date emitted twice (a re-shown page) collapses to one entry
    m = _mega(total_days=5)
    m["entries"] = m["entries"] + [dict(m["entries"][0])]      # duplicate day 1
    m["self_check"]["sum_of_daily_totals"] = 40.0              # consistent claim
    m["stated_total"] = 40.0
    ext, s = _extractor({"*": m})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert sum(e.total for e in r.entries) == 40.0             # not 48
    assert any("deduped" in n for n in r.notes)


def test_repair_triggers_on_internal_mismatch():
    # model's entries sum to 136 but it CLAIMS 160 -> one repair round; the
    # corrected JSON (20 days, consistent) is adopted.
    bad = _mega(total_days=17)
    bad["self_check"]["sum_of_daily_totals"] = 160.0           # slip: claims 160
    bad["stated_total"] = 160.0
    good = _mega(total_days=20, conf=0.9)                      # corrected read
    calls = {"n": 0}

    class _Seq(_FakeClient):
        def chat(self, model, messages, **kw):
            calls["n"] += 1
            import json
            data = bad if calls["n"] == 1 else good
            self.calls.append(model)
            return ChatResponse(text=json.dumps(data), model=model, raw={},
                                usage={"total_tokens": 100, "cost": 0.001})

    ext = DirectExtractor(_FakeRouter(_Seq({})),
                          Settings(flow="direct", direct_verify=False))
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert sum(e.total for e in r.entries) == 160.0            # corrected
    assert any("arithmetic repair" in n for n in r.notes)
    assert calls["n"] == 2                                     # exactly one extra call


def test_no_repair_when_claims_consistent():
    ext, s = _extractor({"*": _mega(conf=0.9)})                # claims == code sum
    ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # single call


def test_sparse_read_escalates_without_printed_total():
    sparse = _mega(total_days=3)                               # 24h grid
    sparse["stated_total"] = None
    sparse["self_check"]["sum_of_daily_totals"] = 24.0
    full = _mega(total_days=20, conf=0.9)
    ext, s = _extractor({"openai/gpt-5.4-nano": sparse,
                         "openai/gpt-5.4-mini": full})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.method == "direct:openai/gpt-5.4-mini"            # climbed on sparsity


def test_sparse_with_printed_total_is_accepted():
    # a true part-time month (sheet prints 24h) must NOT burn escalation
    sparse = _mega(total_days=3)
    sparse["stated_total"] = 24.0
    sparse["self_check"]["sum_of_daily_totals"] = 24.0
    ext, s = _extractor({"*": sparse})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]


def test_strong_model_budget_caps_gpt5():
    low = _mega(conf=0.4)                                      # every rung rejects
    ext, s = _extractor({"*": low}, direct_strong_budget=1)
    # file 1 burns the single gpt-5 slot; file 2 must not call gpt-5
    ext.extract("a.pdf", "a.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    n_gpt5_first = ext.router.client.calls.count("openai/gpt-5")
    ext.extract("b.pdf", "b.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    n_gpt5_total = ext.router.client.calls.count("openai/gpt-5")
    assert n_gpt5_first == 1 and n_gpt5_total == 1             # capped


def test_auto_verify_skips_clean_confident_read():
    ext, s = _extractor({"*": _mega(conf=0.95)},
                        direct_verify=True, direct_verify_mode="auto")
    ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # no verify spent


def test_auto_verify_runs_on_low_confidence():
    ext, s = _extractor(
        {"openai/gpt-5.4-nano": _mega(conf=0.8),
         "verify-model": {"monthly_total": 136, "days_worked": 17, "confidence": 0.9}},
        direct_verify=True, direct_verify_mode="auto",
        direct_verify_model="verify-model")
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert "verify-model" in ext.router.client.calls
    assert r.verification == "confirmed"


def test_eml_input_carries_body_text(tmp_path):
    # an approval email: the body text (with the weekly table) must reach the model
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"] = "Timesheet approval request for May 2026"
    m["From"] = "a@b.com"
    m.set_content("Approved 160 Hours for May for Raviraj\nTotal Hours 160")
    p = tmp_path / "fw.eml"
    p.write_bytes(m.as_bytes())
    ext, s = _extractor({})
    pdf, images, text = ext._as_model_input(p)
    assert pdf is None and images == []
    assert "Approved 160 Hours" in text and "Subject:" in text


# --- disagreement grading + implausible stated-total discard --------------- #
def test_small_cross_model_spread_is_review_not_blocked():
    # nano 136h vs mini 160h... too big; use 157 vs 160 (3h) -> review band
    a = _mega(total_days=20, conf=0.5)                       # 160h, escalates
    b = _mega(total_days=20, conf=0.92)                      # 160h accepted
    # make the second read differ by 3h (one 5h day instead of 8h)
    b["entries"][0]["total_hours"] = 5
    b["self_check"]["sum_of_daily_totals"] = 157.0
    b["stated_total"] = 157.0
    ext, s = _extractor({"openai/gpt-5.4-nano": a, "openai/gpt-5.4-mini": b})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.needs_llm is False                              # NOT hard-blocked
    assert r.confidence <= 0.8                               # -> needs_review
    assert any("differed slightly" in n for n in r.notes)


def test_large_cross_model_spread_still_blocks():
    ext, s = _extractor({
        "openai/gpt-5.4-nano": _mega(total_days=17, conf=0.5),   # 136h
        "openai/gpt-5.4-mini": _mega(total_days=20, conf=0.5),   # 160h
        "openai/gpt-5": _mega(total_days=22, conf=0.5),          # 176h
    })
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.needs_llm is True                               # 40h spread -> blocked


def test_implausible_stated_total_discarded():
    # a full 160h grid with a misread "8h" printed total -> stated dropped, noted
    m = _mega(total_days=20, conf=0.9)
    m["stated_total"] = 8.0
    m["self_check"]["matches_stated_total"] = True           # would have fired repair
    ext, s = _extractor({"*": m})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.stated_total is None
    assert any("implausible printed total" in n for n in r.notes)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # no repair round wasted


def test_plausible_small_stated_total_kept():
    # a genuine part-time month: 24h grid with printed 24 -> stated kept
    m = _mega(total_days=3, conf=0.9)
    m["stated_total"] = 24.0
    ext, s = _extractor({"*": m})
    r = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)[0]
    assert r.stated_total == 24.0
