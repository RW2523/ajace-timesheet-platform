"""End-to-end test against the real sample folder (deterministic path only).

Skips automatically when the sample folder is absent so the suite stays portable.
Point at a different folder with TSE_TEST_FOLDER.
"""
import os
from pathlib import Path

import pytest

from tsengine import process_folder
from tsengine.settings import Settings

DEFAULT = "/Users/richardwatsonstephenamudha/Documents/aj_t/Timesheet"
FOLDER = os.environ.get("TSE_TEST_FOLDER", DEFAULT)

pytestmark = pytest.mark.skipif(
    not Path(FOLDER).exists(), reason=f"sample folder not found: {FOLDER}")


@pytest.fixture(scope="module")
def report():
    # force deterministic mode so the suite is fast, free, and reproducible
    # regardless of any OpenRouter key in the environment/.env
    settings = Settings(llm_policy="never")
    return process_folder(FOLDER, 4, 2026, settings)


def _emp(report, name):
    return next((e for e in report.employees if e.employee_name == name), None)


def test_all_files_attributed(report):
    # every discovered file is either processed or explicitly listed unprocessed
    assert report.files_processed + len(report.unprocessed) == report.files_seen
    assert report.files_seen > 30


def test_known_deterministic_totals(report):
    # full-time monthly grids -> 176h (22 weekdays * 8h)
    for name in ("Harsha", "Siva", "SathiskumarPalanisamy"):
        e = _emp(report, name)
        assert e is not None, name
        assert e.monthly_total == 176.0, (name, e.monthly_total)

    # Richard works 4h/day -> 80h
    richard = _emp(report, "Richard")
    assert richard and richard.monthly_total == 80.0

    # Elangovan labeled-hours timecard -> 136h, no double counting
    el = _emp(report, "Elangovan")
    assert el and el.monthly_total == 136.0

    # Adam (NPO): overtime column captured + Excel-1900-corrupted week recovered
    adam = _emp(report, "Adam")
    assert adam and adam.monthly_regular == 156.0
    assert adam.monthly_overtime == 26.5
    assert adam.monthly_total == 182.5

    # Sean: small overtime (0:30+0:15+0:40) no longer dropped
    sean = _emp(report, "Sean")
    assert sean and sean.monthly_regular == 172.0
    assert sean.monthly_overtime > 0


def test_saravanan_total_mismatch_is_accurate(report):
    # the stated month total (168) -- not a stray weekly subtotal -- is compared
    sara = _emp(report, "Saravanan")
    assert sara is not None
    mm = [i for i in sara.issues if i.code.value == "TOTAL_MISMATCH"]
    assert mm and "168" in mm[0].message


def test_every_employee_has_full_calendar(report):
    for e in report.employees:
        assert len(e.days) == 30
        assert all(d.weekday for d in e.days)


def test_brillio_csv_conflicts_flagged(report):
    yaz = _emp(report, "Yazheni")
    assert yaz is not None
    codes = {i.code.value for i in yaz.all_issues}
    assert "CONFLICT" in codes        # duplicate weekly rows (39 vs 0)
    assert "WEEK_ONLY" in codes       # only weekly granularity


def test_report_serializes(report):
    js = report.model_dump_json()
    assert '"employee_name"' in js
    assert report.stats()["employees"] == len(report.employees)
