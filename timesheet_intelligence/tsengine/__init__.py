"""Timesheet Intelligence Core Engine.

A generic, model-agnostic pipeline that turns a folder of heterogeneous monthly
timesheets (PDF / scanned PDF / Excel / CSV / DOCX / image) into standardized,
audited per-employee monthly records ready for a calendar UI -- without any
template-specific code.
"""
from .pipeline import TimesheetPipeline, process_folder
from .schema import (DayRecord, EmployeeMonth, ProcessingReport, ENGINE_VERSION)
from .settings import get_settings

__all__ = [
    "TimesheetPipeline", "process_folder", "DayRecord", "EmployeeMonth",
    "ProcessingReport", "get_settings", "ENGINE_VERSION",
]
__version__ = ENGINE_VERSION
