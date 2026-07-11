import datetime as dt

from tsengine.normalize import hours as H


def test_parse_decimal_and_clock_style():
    assert H.parse_hours("7.97") == 7.97
    assert H.parse_hours(8) == 8.0
    assert H.parse_hours("8 00") == 8.0      # H MM clock-style cell
    assert H.parse_hours("8:30") == 8.5
    assert H.parse_hours("121 00") == 121.0  # monthly total written as clock
    assert H.parse_hours("7,5") == 7.5       # comma decimal


def test_decimal_hours_not_misread_as_clock():
    # regression (CORE-1): '.' is a decimal point, never a H:MM separator
    assert H.parse_hours("8.50") == 8.5
    assert H.parse_hours("7.25") == 7.25
    assert H.parse_hours("12.30") == 12.3
    assert H.parse_hours("0.50") == 0.5
    assert H.parse_hours("40.00") == 40.0


def test_overnight_shift():
    # regression (CORE-6): a graveyard shift crossing midnight
    assert H.hours_from_in_out("22:00", "06:00") == 8.0
    assert H.hours_from_in_out("23:30", "07:30") == 8.0


def test_parse_hours_blanks_and_junk():
    assert H.parse_hours("") is None
    assert H.parse_hours(None) is None
    assert H.parse_hours("N/A") is None
    assert H.parse_hours("off") is None
    assert H.parse_hours("00:00:00") is None  # three-part time, not an hours qty


def test_parse_time_object_as_duration():
    assert H.parse_hours(dt.time(8, 30)) == 8.5


def test_in_out_pm_crossover():
    # 9:00 -> 5:00 means 9am-5pm = 8h
    assert H.hours_from_in_out("09:00:00", "05:00:00") == 8.0
    assert H.hours_from_in_out("7:30 AM", "4:30 PM", 60) == 8.0
    assert H.hours_from_in_out("00:00:00", "00:00:00") == 0.0
    assert H.hours_from_in_out(None, "5:00") is None


def test_split_regular_overtime():
    # given only regular, total is derived and overtime defaults to 0
    assert H.split_regular_overtime(None, 8.0, None) == (8.0, 0.0, 8.0)
    assert H.split_regular_overtime(10.0, 8.0, None) == (8.0, 2.0, 10.0)
    assert H.split_regular_overtime(8.0, None, None) == (8.0, 0.0, 8.0)
    # nothing derivable stays None
    assert H.split_regular_overtime(None, None, None) == (None, None, None)
