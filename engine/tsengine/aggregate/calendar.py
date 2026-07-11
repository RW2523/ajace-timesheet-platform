"""Calendar scaffolding: every day of the target month, with weekend and
holiday marking.

Holiday rules are intentionally minimal in Phase 1 (a built-in US federal set +
configurable extras). Real overtime/holiday *policy* is deferred to later phases;
here we only *display* the distinctions.
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Optional

from ..schema import DayRecord
from ..settings import Settings, get_settings


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th (1-based) given weekday (0=Mon) of a month."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    last_day = calendar.monthrange(year, month)[1]
    last = dt.date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last - dt.timedelta(days=offset)


class HolidayProvider:
    """Pluggable holiday set. Phase 1 ships US federal holidays; other regions
    fall back to none + whatever extras are configured."""

    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()

    def holidays_for(self, year: int) -> dict[dt.date, str]:
        out: dict[dt.date, str] = {}
        if self.s.holiday_region.upper() == "US":
            out.update(self._us_federal(year))
        for ds in self.s.extra_holiday_list:
            try:
                out[dt.date.fromisoformat(ds)] = "Custom holiday"
            except ValueError:
                pass
        return out

    @staticmethod
    def _us_federal(y: int) -> dict[dt.date, str]:
        return {
            dt.date(y, 1, 1): "New Year's Day",
            _nth_weekday(y, 1, 0, 3): "Martin Luther King Jr. Day",
            _nth_weekday(y, 2, 0, 3): "Presidents' Day",
            _last_weekday(y, 5, 0): "Memorial Day",
            dt.date(y, 6, 19): "Juneteenth",
            dt.date(y, 7, 4): "Independence Day",
            _nth_weekday(y, 9, 0, 1): "Labor Day",
            _nth_weekday(y, 10, 0, 2): "Columbus Day",
            dt.date(y, 11, 11): "Veterans Day",
            _nth_weekday(y, 11, 3, 4): "Thanksgiving Day",
            dt.date(y, 12, 25): "Christmas Day",
        }


_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def build_calendar_days(month: int, year: int,
                        settings: Optional[Settings] = None) -> list[DayRecord]:
    s = settings or get_settings()
    weekend = s.weekend_set
    holidays = HolidayProvider(s).holidays_for(year)
    days: list[DayRecord] = []
    ndays = calendar.monthrange(year, month)[1]
    for dom in range(1, ndays + 1):
        d = dt.date(year, month, dom)
        rec = DayRecord(
            date=d, weekday=_WD[d.weekday()],
            is_weekend=d.weekday() in weekend,
            is_holiday=d in holidays,
        )
        if d in holidays:
            rec.note = holidays[d]
        days.append(rec)
    return days
