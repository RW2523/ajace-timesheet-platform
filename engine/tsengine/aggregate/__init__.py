"""Aggregation: merge per-file normalization results into one calendar-shaped
``EmployeeMonth`` per employee, with weekend/holiday marking and rollups."""
from .registry import EmployeeRegistry
from .calendar import HolidayProvider, build_calendar_days

__all__ = ["EmployeeRegistry", "HolidayProvider", "build_calendar_days"]
