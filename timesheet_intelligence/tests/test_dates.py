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
