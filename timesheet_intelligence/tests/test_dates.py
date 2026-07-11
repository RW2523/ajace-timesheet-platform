import datetime as dt

from tsengine.normalize import dates as D


def test_infer_order_decisive():
    # a component > 12 forces the order
    assert D.infer_date_order(["13/04/2026"], 4, 2026) == "DMY"
    assert D.infer_date_order(["04/13/2026"], 4, 2026) == "MDY"


def test_infer_order_ambiguous_uses_target_month():
    # all components <=12; choose the order that lands in April
    assert D.infer_date_order(["01/04/2026", "02/04/2026"], 4, 2026) == "DMY"
    assert D.infer_date_order(["04/01/2026", "04/02/2026"], 4, 2026) == "MDY"


def test_parse_formats():
    assert D.parse_date("01/04/2026", "DMY", 2026) == dt.date(2026, 4, 1)
    assert D.parse_date("4/16/2026", "MDY", 2026) == dt.date(2026, 4, 16)
    assert D.parse_date("4/30/26", "MDY", 2026) == dt.date(2026, 4, 30)
    assert D.parse_date("1-Apr-26", "MDY", 2026) == dt.date(2026, 4, 1)
    assert D.parse_date("30-Mar-2026", "MDY", 2026) == dt.date(2026, 3, 30)
    assert D.parse_date("2026-04-09", "MDY", 2026) == dt.date(2026, 4, 9)
    assert D.parse_date("April 3, 2026", "MDY", 2026) == dt.date(2026, 4, 3)


def test_parse_garbage_returns_none():
    assert D.parse_date("not a date", "MDY", 2026) is None
    assert D.parse_date("", "MDY", 2026) is None
    assert D.parse_date(None, "MDY", 2026) is None


def test_month_filter_and_overlap():
    assert D.in_target_month(dt.date(2026, 4, 15), 4, 2026)
    assert not D.in_target_month(dt.date(2026, 3, 31), 4, 2026)
    # week 4/26-5/2 -> 5 of 7 days inside April
    frac = D.week_overlap_fraction(dt.date(2026, 4, 26), dt.date(2026, 5, 2), 4, 2026)
    assert round(frac, 3) == round(5 / 7, 3)
    # week fully outside
    assert D.week_overlap_fraction(dt.date(2026, 5, 4), dt.date(2026, 5, 10), 4, 2026) == 0.0


def test_month_days():
    days = D.month_days(4, 2026)
    assert len(days) == 30
    assert days[0] == dt.date(2026, 4, 1)
    assert days[-1] == dt.date(2026, 4, 30)


# --- workday-weighted weekly clip ----------------------------------------- #
def test_clip_weekly_straddle_uses_workdays():
    # Apr 27 (Mon) .. May 3 (Sun); only May 1 (Fri) is an in-May workday
    clip = D.clip_weekly_to_month(dt.date(2026, 4, 27), dt.date(2026, 5, 3),
                                  5, 2026, hours=40.0)
    assert clip.contributing_days == [dt.date(2026, 5, 1)]
    assert round(clip.fraction, 3) == round(1 / 5, 3)     # 1 of 5 workdays
    assert clip.worked_days == 1
    assert not clip.weekend_basis


def test_clip_weekly_fully_in_month():
    clip = D.clip_weekly_to_month(dt.date(2026, 4, 6), dt.date(2026, 4, 12),
                                  4, 2026, hours=40.0)
    assert clip.fraction == 1.0                            # all 5 workdays in April
    assert clip.worked_days == 5


def test_clip_weekly_weekend_fallback():
    # 84h can't fit in 5 workdays -> spread over all 7 days
    clip = D.clip_weekly_to_month(dt.date(2026, 4, 27), dt.date(2026, 5, 3),
                                  5, 2026, hours=84.0)
    assert clip.weekend_basis
    assert set(clip.contributing_days) == {dt.date(2026, 5, 1), dt.date(2026, 5, 2),
                                           dt.date(2026, 5, 3)}
    assert round(clip.fraction, 3) == round(3 / 7, 3)


def test_clip_weekly_respects_covered():
    covered = {dt.date(2026, 5, 1)}
    clip = D.clip_weekly_to_month(dt.date(2026, 4, 27), dt.date(2026, 5, 3),
                                  5, 2026, hours=40.0, covered=covered)
    assert clip.contributing_days == []                    # May 1 already counted
    assert clip.fraction == 0.0


def test_clip_weekly_custom_weekend():
    # Fri/Sat weekend (weekend={4,5}); the in-month workday shifts accordingly
    clip = D.clip_weekly_to_month(dt.date(2026, 4, 27), dt.date(2026, 5, 3),
                                  5, 2026, hours=40.0, weekend={4, 5})
    # May 1 is Fri (now weekend), May 3 is Sun (workday) -> Sun is the in-month workday
    assert dt.date(2026, 5, 3) in clip.contributing_days
    assert dt.date(2026, 5, 1) not in clip.contributing_days


# --- filename / content period signals ------------------------------------ #
def test_month_from_filename_spelled():
    assert D.month_from_filename("Ravi TS May -2026.pdf", 2026) == (5, 2026)
    assert D.month_from_filename("Elangovan TS May-2026.pdf", 2026) == (5, 2026)
    assert D.month_from_filename("Saravanan TimeSheet-April 2026.xlsx") == (4, 2026)


def test_month_from_filename_numeric_and_iso():
    assert D.month_from_filename("time for 4.30.26.pdf", 2026) == (4, 2026)
    assert D.month_from_filename("TangiralaG-Timesheet-20260529.xlsx") == (5, 2026)


def test_month_from_filename_none():
    assert D.month_from_filename("scan_final_copy.png") is None
    assert D.month_from_filename("") is None


def test_dominant_month():
    days = [dt.date(2026, 5, d) for d in range(1, 20)] + [dt.date(2026, 4, 30)]
    assert D.dominant_month(days) == (5, 2026)
    # a lone date is not enough
    assert D.dominant_month([dt.date(2026, 5, 1)]) is None
    # a 50/50 split has no dominant month
    split = [dt.date(2026, 4, 1), dt.date(2026, 4, 2),
             dt.date(2026, 5, 1), dt.date(2026, 5, 2)]
    assert D.dominant_month(split) is None


# --- 2-of-3 period resolver ------------------------------------------------ #
def test_resolve_period_confirmed_by_majority():
    sheet = [dt.date(2026, 5, d) for d in range(1, 20)]
    r = D.resolve_target_period(5, 2026, filename="Ravi TS May -2026.pdf",
                                sheet_dates=sheet)
    assert r.status == "confirmed"
    assert (r.month, r.year) == (5, 2026)
    assert not r.mismatch


def test_resolve_period_remapped_when_filename_and_sheet_agree():
    # requested May, but a file that is entirely an April timesheet
    sheet = [dt.date(2026, 4, d) for d in range(1, 20)]
    r = D.resolve_target_period(5, 2026, filename="time for 4.30.26.pdf",
                                sheet_dates=sheet)
    assert r.status == "remapped"
    assert (r.month, r.year) == (4, 2026)
    assert r.mismatch and r.note


def test_resolve_period_conflict_all_disagree():
    sheet = [dt.date(2026, 3, d) for d in range(1, 6)]     # sheet says March
    r = D.resolve_target_period(5, 2026, filename="ts-jan-2026.pdf",
                                sheet_dates=sheet)
    assert r.status == "conflict"
    assert (r.month, r.year) == (5, 2026)                  # keeps requested
    assert r.mismatch


def test_resolve_period_requested_only():
    r = D.resolve_target_period(5, 2026, filename="scan.png", sheet_dates=[])
    assert r.status == "confirmed"
    assert (r.month, r.year) == (5, 2026)
