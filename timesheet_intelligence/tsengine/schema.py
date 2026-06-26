"""Canonical data model for the Timesheet Intelligence Core Engine.

Two layers:

* **Raw layer** (`RawExtraction`, `RawTable`) -- whatever an ingest path could
  pull out of a file (text, tables, images), with source references attached.
  This is intentionally permissive: it is the common currency between the many
  format-specific extractors and the normalizer.

* **Canonical layer** (`EmployeeMonth`, `DayRecord`, ...) -- the standardized,
  audited result the rest of the system (validation, calendar, UI, future
  payroll) consumes. Every extracted value keeps a trail of `SourceRef`s so any
  number on screen can be traced back to a filename/page/sheet/row/region.

Nothing here is template-specific; it is the same shape for an Excel grid, a
scanned PDF, or an LLM-interpreted handwritten image.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

ENGINE_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Source references & issues (auditing)
# --------------------------------------------------------------------------- #
class SourceRef(BaseModel):
    """Pointer back to the exact location a value came from."""

    file: str
    page: Optional[int] = None          # 1-based page (PDF / image)
    sheet: Optional[str] = None         # spreadsheet sheet name
    row: Optional[int] = None           # 1-based row (spreadsheet / CSV / table)
    column: Optional[str] = None        # column header or letter
    cell: Optional[str] = None          # e.g. "C12"
    region: Optional[str] = None        # bbox "x0,y0,x1,y1" or human description
    extractor: Optional[str] = None     # which ingest path produced this
    note: Optional[str] = None

    def label(self) -> str:
        parts = [self.file]
        if self.sheet:
            parts.append(f"sheet={self.sheet}")
        if self.page is not None:
            parts.append(f"p{self.page}")
        if self.cell:
            parts.append(self.cell)
        elif self.row is not None:
            parts.append(f"row{self.row}")
        if self.region:
            parts.append(f"@{self.region}")
        return " · ".join(parts)


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueCode(str, Enum):
    MISSING = "MISSING"                 # no entry for a working day
    INVALID = "INVALID"                 # unparseable / nonsensical value
    UNCLEAR = "UNCLEAR"                 # low-confidence / ambiguous reading
    DUPLICATE = "DUPLICATE"             # same day reported more than once (same value)
    CONFLICT = "CONFLICT"              # same day reported with different values
    OUT_OF_RANGE = "OUT_OF_RANGE"      # hours outside sane bounds
    CROSS_MONTH = "CROSS_MONTH"        # source period spans outside target month
    WEEK_ONLY = "WEEK_ONLY"            # only weekly totals available, no daily split
    TOTAL_MISMATCH = "TOTAL_MISMATCH"  # stated total != sum of parts
    NEEDS_LLM = "NEEDS_LLM"            # deterministic path insufficient; LLM/vision recommended
    UNATTRIBUTED = "UNATTRIBUTED"      # could not tie file to an employee
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"
    PARSE_ERROR = "PARSE_ERROR"


class Issue(BaseModel):
    code: IssueCode
    severity: IssueSeverity = IssueSeverity.WARNING
    message: str
    date: Optional[dt.date] = None
    sources: list[SourceRef] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Canonical timesheet model
# --------------------------------------------------------------------------- #
class DayRecord(BaseModel):
    """One calendar day in the target month for one employee."""

    date: dt.date
    weekday: str                       # "Mon".."Sun"
    is_weekend: bool = False
    is_holiday: bool = False

    regular_hours: Optional[float] = None
    overtime_hours: Optional[float] = None
    total_hours: Optional[float] = None

    client: Optional[str] = None       # originating client for the day
    project: Optional[str] = None      # dominant project/client for the day
    note: Optional[str] = None
    raw: Optional[str] = None          # raw evidence text for the day

    sources: list[SourceRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return any(
            v is not None for v in (self.regular_hours, self.overtime_hours, self.total_hours)
        )

    @property
    def flagged(self) -> bool:
        return bool(self.issues)


class ClientBreakdown(BaseModel):
    client: Optional[str] = None
    project: Optional[str] = None
    regular_hours: float = 0.0
    overtime_hours: float = 0.0
    total_hours: float = 0.0
    days_worked: int = 0


class WeeklyTotal(BaseModel):
    """Retained when a source only provides weekly (not daily) numbers."""

    week_start: dt.date
    week_end: dt.date
    regular_hours: Optional[float] = None
    overtime_hours: Optional[float] = None
    total_hours: Optional[float] = None
    in_month_fraction: float = 1.0     # portion of the week inside the target month
    sources: list[SourceRef] = Field(default_factory=list)
    note: Optional[str] = None


class EmployeeMonth(BaseModel):
    """Standardized monthly record for one employee."""

    employee_name: Optional[str] = None
    employee_id: Optional[str] = None
    clients: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)

    month: int
    year: int

    days: list[DayRecord] = Field(default_factory=list)
    weekly_totals: list[WeeklyTotal] = Field(default_factory=list)

    monthly_regular: float = 0.0
    monthly_overtime: float = 0.0
    monthly_total: float = 0.0
    days_worked: int = 0

    client_breakdown: list[ClientBreakdown] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    source_files: list[str] = Field(default_factory=list)
    extraction_methods: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    @property
    def all_issues(self) -> list[Issue]:
        out = list(self.issues)
        for d in self.days:
            out.extend(d.issues)
        return out


class UnprocessedFile(BaseModel):
    file: str
    reason: str
    file_type: Optional[str] = None


class ProcessingReport(BaseModel):
    engine_version: str = ENGINE_VERSION
    folder: str
    month: int
    year: int
    generated_at: str
    employees: list[EmployeeMonth] = Field(default_factory=list)
    unprocessed: list[UnprocessedFile] = Field(default_factory=list)

    # rollups
    files_seen: int = 0
    files_processed: int = 0
    llm_used: bool = False

    # OpenRouter usage / cost accounting
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0
    llm_usage_by_model: dict[str, Any] = Field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        return {
            "employees": len(self.employees),
            "files_seen": self.files_seen,
            "files_processed": self.files_processed,
            "unprocessed": len(self.unprocessed),
            "total_hours": round(sum(e.monthly_total for e in self.employees), 2),
            "issues": sum(len(e.all_issues) for e in self.employees),
            "llm_used": self.llm_used,
        }


# --------------------------------------------------------------------------- #
# Raw extraction layer (ingest -> normalize)
# --------------------------------------------------------------------------- #
class FileKind(str, Enum):
    EXCEL = "excel"
    CSV = "csv"
    PDF_NATIVE = "pdf_native"
    PDF_SCANNED = "pdf_scanned"
    DOCX = "docx"
    IMAGE = "image"
    EMAIL = "email"
    UNKNOWN = "unknown"


class ExtractionQuality(str, Enum):
    NATIVE = "native"     # structured / digital text -- high trust
    OCR = "ocr"           # local OCR -- medium trust
    VISION = "vision"     # LLM vision -- medium/high trust
    EMPTY = "empty"       # nothing usable extracted


class RawTable(BaseModel):
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    source: SourceRef
    title: Optional[str] = None        # sheet name / nearby caption


class RawImage(BaseModel):
    path: str                          # filesystem path to a rasterized/embedded image
    source: SourceRef
    width: Optional[int] = None
    height: Optional[int] = None


class RawExtraction(BaseModel):
    """Everything one ingest path could pull from a single file."""

    file: str
    kind: FileKind
    quality: ExtractionQuality = ExtractionQuality.NATIVE
    text: str = ""
    tables: list[RawTable] = Field(default_factory=list)
    images: list[RawImage] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)   # name/client hints, page count, etc.
    sources: list[SourceRef] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def has_structured(self) -> bool:
        return bool(self.tables)

    @property
    def has_text(self) -> bool:
        return len(self.text.strip()) > 0
