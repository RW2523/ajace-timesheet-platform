import datetime as dt

from tsengine.normalize.normalizer import (Normalizer, _assign_roles,
                                           _strategy_daily_grid,
                                           _strategy_text_hours_labeled)
from tsengine.schema import (ExtractionQuality, FileKind, RawExtraction,
                             RawTable, SourceRef)


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
