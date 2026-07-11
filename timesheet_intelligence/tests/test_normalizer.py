import datetime as dt

from tsengine.aggregate.registry import EmployeeRegistry
from tsengine.normalize.normalizer import (DayEntry, NormResult, Normalizer,
                                           _apply_vote_validity, _assign_roles,
                                           _strategy_daily_grid,
                                           _strategy_portal_periods,
                                           _strategy_text_hours_labeled)
from tsengine.schema import (ExtractionQuality, FileKind, RawExtraction,
                             RawTable, SourceRef, WeeklyTotal)

_WKND = {5, 6}


def _mk(method, entries=(), weekly=(), stated=None, conf=1.0):
    return NormResult(file="f", method=method, quality=ExtractionQuality.NATIVE,
                      entries=list(entries), weekly_totals=list(weekly),
                      stated_total=stated, confidence=conf)


def test_overtime_role_not_stolen_by_generic_hours():
    # regression: "Number of Overtime Hours" must map to overtime, not 'hours'
    hdr = ["DAY", "DATE", "TIME IN", "TIME OUT",
           "NUMBER OF REGULAR HOURS", "NUMBER OF OVERTIME HOURS", "NOTES"]
    roles = _assign_roles(hdr)
    assert roles.get("regular") == 4
    assert roles.get("overtime") == 5


def test_daily_grid_captures_overtime_and_recovers_epoch_dates():
    # regression for the Adam case: OT column + an Excel-1900-corrupted week
    rows = [
        ["DAY", "DATE", "TIME IN", "TIME OUT",
         "NUMBER OF REGULAR HOURS", "NUMBER OF OVERTIME HOURS", "NOTES"],
        ["MON", "2026-04-06", "06:00:00", "16:00:00", 8, 2, "x"],
        ["TUE", "1900-01-21", "06:00:00", "18:00:00", 8, 4, "corrupted date"],
    ]
    t = RawTable(rows=rows, title="wk", source=SourceRef(file="adam.xlsx", sheet="wk"))
    res = _strategy_daily_grid(t, "MDY", 4, 2026, 16.0)
    by_date = {e.date: e for e in res.entries}
    assert by_date[dt.date(2026, 4, 6)].overtime == 2.0
    # 1900-01-21 recovered to 2026-04-21 (day-of-month preserved)
    assert dt.date(2026, 4, 21) in by_date
    assert by_date[dt.date(2026, 4, 21)].overtime == 4.0


def _grid_extraction():
    grid = [
        ["", "", "", "", "", "", "Monthly Time Record"],
        ["Day", "Date", "In", "Out", "", "", "Regular Hrs.", "Sick Hrs.",
         "Vacation Hrs.", "Total Hrs."],
        ["Wed", "2026-04-01", "09:00:00", "05:00:00", "", "", 8.0, "", "", 8.0],
        ["Thu", "2026-04-02", "09:00:00", "05:00:00", "", "", 8.0, "", "", 8.0],
        ["Sat", "2026-04-04", "", "", "", "", "", "", "", ""],   # blank weekend row
        ["Mon", "2026-04-06", "10:00:00", "14:00:00", "", "", "", "", "", ""],  # in/out only
    ]
    raw = RawExtraction(file="emp.xlsx", kind=FileKind.EXCEL)
    raw.tables.append(RawTable(rows=grid, title="Sheet1",
                               source=SourceRef(file="emp.xlsx", sheet="Sheet1")))
    raw.meta["name_hint"] = "Emp"
    return raw


def test_daily_grid_strategy():
    res = Normalizer().normalize(_grid_extraction(), 4, 2026, client_hint="ACME")
    assert len(res) == 1
    r = res[0]
    by_date = {e.date: e for e in r.entries}
    assert by_date[dt.date(2026, 4, 1)].total == 8.0
    assert by_date[dt.date(2026, 4, 2)].regular == 8.0
    # in/out only row computed: 10:00-14:00 = 4h
    assert by_date[dt.date(2026, 4, 6)].total == 4.0
    # blank weekend row must NOT appear and must NOT be flagged
    assert dt.date(2026, 4, 4) not in by_date
    assert r.client == "ACME"
    assert r.confidence >= 0.9


def test_weekly_totals_strategy():
    raw = RawExtraction(file="wk.csv", kind=FileKind.CSV)
    raw.tables.append(RawTable(
        headers=["Timesheet Start Date", "Timesheet End Date", "ST/FP/Bench Hours",
                 "OT Hours", "Total Hours"],
        rows=[["4/12/2026", "4/18/2026", "39", "0", "39"],
              ["4/26/2026", "5/2/2026", "32", "0", "32"]],
        source=SourceRef(file="wk.csv")))
    res = Normalizer().normalize(raw, 4, 2026)[0]
    assert len(res.weekly_totals) == 2
    w2 = [w for w in res.weekly_totals if w.week_start == dt.date(2026, 4, 26)][0]
    assert round(w2.in_month_fraction, 3) == round(5 / 7, 3)


def test_text_hours_labeled_collapses_duplicate_views():
    # same date appears twice with identical labeled hours -> counted once
    text = ("Period 30-Mar-2026 - 05-Apr-2026\n"
            "01-Apr-2026\n8.00 Hours\nProject X\n"
            "02-Apr-2026\n8.00 Hours\nProject X\n"
            "01-Apr-2026\n8.00 Hours\nSummary\n")
    res = _strategy_text_hours_labeled(text, "tc.pdf", "MDY", 4, 2026, 16.0)
    by_date = {e.date: e for e in res.entries}
    assert by_date[dt.date(2026, 4, 1)].total == 8.0   # not 16
    assert by_date[dt.date(2026, 4, 2)].total == 8.0


def test_text_hours_labeled_flags_conflicting_values():
    text = "01-Apr-2026\n8.00 Hours\n01-Apr-2026\n4.00 Hours\n"
    res = _strategy_text_hours_labeled(text, "tc.pdf", "MDY", 4, 2026, 16.0)
    e = res.entries[0]
    assert e.total is None
    assert any(i.code.value == "UNCLEAR" for i in e.issues)


# --- portal-export period parsing (step 2: week dedupe) ------------------- #
# A biweekly portal export whose per-period total row is OCR'd 3x per page.
# The flat sum-verified strategy would add every repeat -> 448h; the period
# strategy anchors totals to their date range, dedupes the repeats, and the
# rollup clips each period to the month by workday.
_SAURABH_OCR = """----- page 1 (OCR) -----
Approved FNMATSO1500615 04/19/2026 to 05/02/2026 Fannie Mae
ST /Hr - 8.00 8.00 8.00 8.00 8.00 8.00 8.00 8.00 8.00 8.00 80.00
Billable Total 0.00 8.00 8.00 8.00 8.00 8.00 0.00 0.00 8.00 8.00 8.00 8.00 8.00 0.00 80.00
0.00 8.00 8.00 8.00 8.00 8.00 0.00 0.00 8.00 8.00 8.00 8.00 8.00 0.00 80.00
----- page 2 (OCR) -----
Approved FNMATSO1502613 05/03/2026 to 05/16/2026 Fannie Mae
Billable Total 0.00 8.00 8.00 8.00 8.00 8.00 0.00 0.00 8.00 8.00 8.00 8.00 8.00 0.00 80.00
----- page 3 (OCR) -----
Approved FNMATSO1502614 05/17/2026 to 05/30/2026 Fannie Mae
Billable Total 0.00 8.00 8.00 8.00 8.00 8.00 0.00 0.00 0.00 8.00 8.00 8.00 0.00 0.00 64.00
Day Total 0.00 8.00 8.00 8.00 8.00 8.00 0.00 0.00 0.00 8.00 8.00 8.00 0.00 0.00 64.00
"""


def test_portal_periods_dedupes_repeated_totals():
    res = _strategy_portal_periods(_SAURABH_OCR, "saurabh.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    assert res is not None
    # three distinct periods, each counted once (not the 3x/2x OCR repeats)
    spans = sorted((w.week_start, w.week_end, w.total_hours) for w in res.weekly_totals)
    assert spans == [
        (dt.date(2026, 4, 19), dt.date(2026, 5, 2), 80.0),
        (dt.date(2026, 5, 3), dt.date(2026, 5, 16), 80.0),
        (dt.date(2026, 5, 17), dt.date(2026, 5, 30), 64.0),
    ]


def test_portal_periods_rollup_clips_to_month():
    # end-to-end: the 448h flat-sum bug becomes the correct clipped 152h.
    res = _strategy_portal_periods(_SAURABH_OCR, "saurabh.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    res.employee_name = "Saurabh Limje"
    em = EmployeeRegistry().build([res], 5, 2026)[0]
    # P1 04/19-05/02 -> only May 1 in-month (8h); P2 full 80h; P3 full 64h
    assert round(em.monthly_total, 2) == 152.0


def test_portal_periods_keeps_distinct_equal_weeks():
    # THE false-merge guard: five identical 40h weeks with DISTINCT date ranges
    # must all survive (200h), never collapse to one. Dedup keys on date range,
    # never on hours.
    weeks = [("05/04/2026", "05/08/2026"), ("05/11/2026", "05/15/2026"),
             ("05/18/2026", "05/22/2026"), ("05/25/2026", "05/29/2026"),
             ("06/01/2026", "06/05/2026")]
    text = "".join(
        f"----- page {i+1} (OCR) -----\nPeriod {a} to {b}\n"
        f"Total 8.00 8.00 8.00 8.00 8.00 40.00\n"
        for i, (a, b) in enumerate(weeks))
    res = _strategy_portal_periods(text, "grinder.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    assert res is not None
    assert len(res.weekly_totals) == 5           # not merged to 1
    res.employee_name = "Distinct Weeks"
    em = EmployeeRegistry().build([res], 5, 2026)[0]
    assert round(em.monthly_total, 2) == 160.0   # 4 May weeks * 40; June week clipped out


def test_portal_periods_none_without_period_structure():
    # a plain WK1..WK5 scan has no "<date> to <date>" header -> falls through
    plain = "WK1 WK2 WK3 WK4 Total\n40 40 40 40 160\n"
    assert _strategy_portal_periods(plain, "scan.png",
                                    ExtractionQuality.OCR, "MDY", 5, 2026) is None


def test_portal_periods_textual_month_and_project_subtotals():
    # Jira-style: "Apr 26, 2026 - May 2, 2026" + per-project subtotal rows plus a
    # day-total row. MAX-per-period keeps the 40h weekly total, not 37.5+2.5+40.
    jira = (
        "----- page 1 (OCR) -----\nApr 26, 2026 - May 2, 2026 Weekly\n"
        "Magnolia - MCMS 0 7.50 7.50 7.50 7.50 7.50 0 37.50\n"
        "Portfolio Management - PM 0 0.50 0.50 0.50 0.50 0.50 0 2.50\n"
        "0 8.00 8.00 8.00 8.00 8.00 0 40.00\n"
        "----- page 2 (OCR) -----\nMay 3, 2026 - May 9, 2026 Weekly\n"
        "0 8.00 8.00 8.00 8.00 8.00 0 40.00\n")
    res = _strategy_portal_periods(jira, "jira.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    assert [w.total_hours for w in res.weekly_totals] == [40.0, 40.0]
    assert res.weekly_totals[0].week_start == dt.date(2026, 4, 26)


def test_portal_periods_year_only_on_end_date():
    # Beeline-style: the start side has no year ("Apr 25 - May 01, 2026").
    beeline = (
        "----- page 1 (OCR) -----\nApr 25 - May 01, 2026 Locked\n"
        "0 8.00 8.00 8.00 8.00 8.00 0 40.00\n")
    res = _strategy_portal_periods(beeline, "beeline.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    assert res.weekly_totals[0].week_start == dt.date(2026, 4, 25)
    assert res.weekly_totals[0].week_end == dt.date(2026, 5, 1)


def test_portal_periods_ignores_interfering_assignment_range():
    # Beeline prints a >1yr "Date Range 2/7/2026 - 2/18/2027" assignment line on
    # every page, between the real period header and its total row. It must be
    # stepped over, not allowed to orphan the period's total.
    page = (
        "----- page {n} (OCR) -----\n{a} - {b}, 2026\n"
        "Date Range 2/7/2026 - 2/18/2027\n"
        "Sat Sun Mon Tue Wed Thu Fri TOTAL\n"
        "Regular Time 8 8 8 8 8 40\nTOTAL HOURS 0 0 8 8 8 8 8 40\n")
    text = (page.format(n=1, a="Apr 25", b="May 01")
            + page.format(n=2, a="May 02", b="May 08")
            + page.format(n=3, a="May 09", b="May 15"))
    res = _strategy_portal_periods(text, "jude.pdf",
                                   ExtractionQuality.OCR, "MDY", 5, 2026)
    assert res is not None
    assert [w.total_hours for w in res.weekly_totals] == [40.0, 40.0, 40.0]
    em = EmployeeRegistry().build(
        [setattr(res, "employee_name", "Jude") or res], 5, 2026)[0]
    # Apr25-May01 -> May 1 only (8h); May02-08 & May09-15 full -> 88h
    assert round(em.monthly_total, 2) == 88.0


# --- vote-validity: a lone/partial read may not silently decide a month ---- #
def test_vote_validity_flags_lone_stated_total():
    # a legacy .xls whose only value is one summary cell (Justin 8h vs 170h)
    r = _mk("summary_total", stated=8.0, conf=0.6)
    _apply_vote_validity(r, 5, 2026, _WKND)
    assert r.needs_llm and r.confidence <= 0.25


def test_vote_validity_flags_single_day_read():
    # a docx where one "8 Hours" label was read (Hemachandra 8h vs 168h)
    r = _mk("text_hours_labeled",
            entries=[DayEntry(date=dt.date(2026, 5, 1), total=8.0)], conf=0.72)
    _apply_vote_validity(r, 5, 2026, _WKND)
    assert r.needs_llm and r.confidence <= 0.25


def test_vote_validity_keeps_full_grid():
    # a real 12-day grid (72h) is evidence-valid -> untouched
    ents = [DayEntry(date=dt.date(2026, 5, d), total=6.0) for d in range(1, 13)]
    r = _mk("daily_grid", entries=ents, conf=0.9)
    _apply_vote_validity(r, 5, 2026, _WKND)
    assert not r.needs_llm and r.confidence == 0.9


def test_vote_validity_exempts_verified_methods():
    # a deduped portal period or sum-verified OCR total is self-checking even if
    # the in-month portion is small -> never demoted
    r = _mk("portal_periods",
            weekly=[WeeklyTotal(week_start=dt.date(2026, 4, 27),
                                week_end=dt.date(2026, 5, 3), total_hours=40.0)],
            conf=0.62)
    _apply_vote_validity(r, 5, 2026, _WKND)
    assert not r.needs_llm and r.confidence == 0.62


def test_vote_validity_keeps_multiweek_weekly_totals():
    # two full in-month weeks (>=10 weekdays) stand on their own
    wk = [WeeklyTotal(week_start=dt.date(2026, 5, 4), week_end=dt.date(2026, 5, 8),
                      total_hours=40.0),
          WeeklyTotal(week_start=dt.date(2026, 5, 11), week_end=dt.date(2026, 5, 15),
                      total_hours=40.0)]
    r = _mk("weekly_totals", weekly=wk, conf=0.7)
    _apply_vote_validity(r, 5, 2026, _WKND)
    assert not r.needs_llm and r.confidence == 0.7


# --- step 6: approval-email lane ------------------------------------------ #
from tsengine.normalize.normalizer import (_email_like,           # noqa: E402
                                           _strategy_email_approval)


def test_email_like_detects_headers_and_eml():
    body = "From: a@b.com\nSubject: Timesheet approval\nApproved 160 hours\n"
    assert _email_like(FileKind.PDF_NATIVE, body)
    assert _email_like(FileKind.EMAIL, "")               # a real .eml always
    # a plain scanned timesheet is NOT an email even if it says "approved"
    assert not _email_like(FileKind.PDF_SCANNED,
                           "Approved FNMATSO1500615 04/19/2026 to 05/02/2026")


def test_email_approval_extracts_stated_total_as_testimony():
    text = ("Subject: Timesheet approval request for May 2026\n"
            "From: Neeta\nApproved 160 Hours for May for Raviraj\n"
            "Total Hours 160\n")
    r = _strategy_email_approval(text, "fw.eml", 5, 2026)
    assert r is not None and r.stated_total == 160.0
    assert r.method == "email_approval"
    assert 0.6 <= r.confidence < 0.85                     # review band, never auto
    assert any("testimony" in n for n in r.notes)


def test_email_approval_picks_the_most_stated_figure():
    text = ("From: x\nSent: today\nApproved 160 hours\n"
            "Total Hours 160\nsome noise 8 hours mentioned once\n")
    r = _strategy_email_approval(text, "e.eml", 5, 2026)
    assert r.stated_total == 160.0                        # 160 stated twice, not 8


def test_email_approval_none_without_a_figure():
    assert _strategy_email_approval("From: x\nSubject: hi\nno hours here\n",
                                    "e.eml", 5, 2026) is None
