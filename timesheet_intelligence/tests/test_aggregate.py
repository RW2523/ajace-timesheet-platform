import datetime as dt

from tsengine.aggregate.calendar import HolidayProvider, build_calendar_days
from tsengine.aggregate.registry import EmployeeRegistry
from tsengine.normalize.normalizer import DayEntry, NormResult
from tsengine.schema import ExtractionQuality, SourceRef, WeeklyTotal


def test_calendar_weekend_and_holiday_marking():
    days = build_calendar_days(4, 2026)
    assert len(days) == 30
    apr4 = next(d for d in days if d.date == dt.date(2026, 4, 4))
    assert apr4.is_weekend                      # Saturday
    # US holiday set includes Memorial Day etc.; April has none by default
    hol = HolidayProvider().holidays_for(2026)
    assert dt.date(2026, 7, 4) in hol
    assert dt.date(2026, 1, 1) in hol


def _res(file, date, total, extractor="daily_grid"):
    return NormResult(
        file=file, method=extractor, quality=ExtractionQuality.NATIVE,
        employee_name="Sam Smith",
        entries=[DayEntry(date=date, total=total,
                          source=SourceRef(file=file, extractor=extractor))],
        confidence=0.9)


def test_conflict_resolution_prefers_higher_trust():
    # same day, two sources, different totals -> native (daily_grid) beats ocr
    high = _res("a.xlsx", dt.date(2026, 4, 1), 8.0, "daily_grid")
    low = _res("b.png", dt.date(2026, 4, 1), 5.0, "image")
    em = EmployeeRegistry().build([high, low], 4, 2026)[0]
    apr1 = next(d for d in em.days if d.date == dt.date(2026, 4, 1))
    assert apr1.total_hours == 8.0
    assert any(i.code.value == "CONFLICT" for i in apr1.issues)


def test_duplicate_same_value_is_info_only():
    a = _res("a.xlsx", dt.date(2026, 4, 1), 8.0)
    b = _res("b.xlsx", dt.date(2026, 4, 1), 8.0)
    em = EmployeeRegistry().build([a, b], 4, 2026)[0]
    apr1 = next(d for d in em.days if d.date == dt.date(2026, 4, 1))
    assert apr1.total_hours == 8.0
    assert any(i.code.value == "DUPLICATE" for i in apr1.issues)
    assert not any(i.code.value == "CONFLICT" for i in apr1.issues)


def test_weekly_only_prorated_into_month():
    r = NormResult(file="w.csv", method="weekly_totals",
                   quality=ExtractionQuality.NATIVE, employee_name="Week Worker",
                   weekly_totals=[WeeklyTotal(
                       week_start=dt.date(2026, 4, 26), week_end=dt.date(2026, 5, 2),
                       total_hours=35.0, in_month_fraction=5 / 7,
                       sources=[SourceRef(file="w.csv")])],
                   confidence=0.7)
    em = EmployeeRegistry().build([r], 4, 2026)[0]
    assert round(em.monthly_total, 2) == 25.0     # 35 * 5/7
    assert any(i.code.value == "WEEK_ONLY" for i in em.issues)


def test_overlapping_weekly_sources_not_double_counted():
    # same person, two SOURCES reporting the SAME weeks -> count each day once
    wk = lambda s, e, h, f: WeeklyTotal(week_start=dt.date(2026, 4, s),
                                        week_end=dt.date(2026, 4, e), total_hours=h,
                                        in_month_fraction=1.0,
                                        sources=[SourceRef(file=f)])
    csv = NormResult(file="a.csv", method="weekly_totals",
                     quality=ExtractionQuality.NATIVE, employee_name="Dup Person",
                     weekly_totals=[wk(6, 12, 40, "a.csv"), wk(13, 19, 40, "a.csv")],
                     confidence=0.7)
    jpg = NormResult(file="b.jpg", method="weekly_totals",
                     quality=ExtractionQuality.VISION, employee_name="Dup Person",
                     weekly_totals=[wk(6, 12, 40, "b.jpg"), wk(13, 19, 40, "b.jpg")],
                     confidence=0.7)
    em = EmployeeRegistry().build([csv, jpg], 4, 2026)[0]
    # both sources cover Apr 6-19 -> 80h total, NOT 160h (not double counted)
    assert em.monthly_total == 80.0


def test_multiple_files_one_employee_merge():
    r1 = _res("week1.xlsx", dt.date(2026, 4, 1), 8.0)
    r2 = _res("week2.xlsx", dt.date(2026, 4, 8), 8.0)
    ems = EmployeeRegistry().build([r1, r2], 4, 2026)
    assert len(ems) == 1                          # merged by name
    assert ems[0].monthly_total == 16.0
    assert len(ems[0].source_files) == 2
