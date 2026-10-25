from __future__ import annotations

import datetime as dt

import pytest

from depmortality.labels.triggers import (
    INACTIVE_CLASSIFIER,
    Advisory,
    Release,
    TriggerEvent,
    WheelTag,
    advisory_trigger,
    archived_trigger,
    first_classifier_day,
    first_failure,
    parse_wheel_filename,
    python_support_trigger,
    statement_trigger,
)

D = dt.date
FREEZE = D(2026, 10, 31)
PY = {"3.10": D(2021, 10, 4), "3.11": D(2022, 10, 24), "3.12": D(2023, 10, 2)}
PURE = (WheelTag("py3", "none"),)


def rel(version: str, day: dt.date, **kw: object) -> Release:
    return Release(version, day, **kw)  # type: ignore[arg-type]


# ---- (a) -------------------------------------------------------------------------------
def test_archived_dated_from_github() -> None:
    t = archived_trigger(True, D(2020, 5, 1), None, FREEZE)
    assert t == TriggerEvent("archived", D(2020, 5, 1), "github archived")


def test_archived_without_date_is_undated() -> None:
    t = archived_trigger(True, None, None, FREEZE)
    assert t is not None and t.day is None


def test_pypi_deprecated_status_undated_and_quarantine_ignored() -> None:
    t = archived_trigger(False, None, "deprecated", FREEZE)
    assert t is not None and t.day is None
    assert archived_trigger(False, None, "quarantined", FREEZE) is None
    assert archived_trigger(False, None, "active", FREEZE) is None


def test_archived_after_freeze_ignored() -> None:
    assert archived_trigger(True, D(2027, 1, 1), None, FREEZE) is None


# ---- (b) -------------------------------------------------------------------------------
RELEASES = [rel("1.0", D(2019, 1, 1)), rel("1.1", D(2020, 1, 1))]


@pytest.mark.parametrize(("fix_after_days", "fires"), [(89, False), (90, False), (91, True)])
def test_advisory_window_boundary(fix_after_days: int, fires: bool) -> None:
    published = D(2021, 3, 1)
    fix = rel("1.2", published + dt.timedelta(days=fix_after_days))
    adv = Advisory("GHSA-xxxx", published, fixed=("1.2",))
    t = advisory_trigger([adv], [*RELEASES, fix], 90, FREEZE)
    assert (t is not None) == fires
    if t is not None:
        assert t.day == published + dt.timedelta(days=90)


def test_advisory_fixed_before_publication_is_not_failure() -> None:
    adv = Advisory("PYSEC-1", D(2021, 6, 1), fixed=("1.1",))
    assert advisory_trigger([adv], RELEASES, 90, FREEZE) is None


def test_advisory_with_no_fix_fires() -> None:
    adv = Advisory("PYSEC-2", D(2021, 6, 1))
    t = advisory_trigger([adv], RELEASES, 90, FREEZE)
    assert t is not None and t.day == D(2021, 8, 30)


def test_last_affected_fix_is_next_release() -> None:
    adv = Advisory("GHSA-la", D(2021, 6, 1), last_affected=("1.1",))
    assert advisory_trigger([adv], [*RELEASES, rel("1.2", D(2021, 7, 1))], 90, FREEZE) is None


def test_yanked_fix_does_not_count() -> None:
    adv = Advisory("GHSA-y", D(2021, 6, 1), fixed=("1.2",))
    t = advisory_trigger([adv], [*RELEASES, rel("1.2", D(2021, 6, 5), yanked=True)], 90, FREEZE)
    assert t is not None


def test_advisory_near_freeze_is_censored() -> None:
    adv = Advisory("GHSA-late", FREEZE - dt.timedelta(days=30))
    assert advisory_trigger([adv], RELEASES, 90, FREEZE) is None


def test_withdrawn_advisory_ignored() -> None:
    adv = Advisory("GHSA-w", D(2021, 6, 1), withdrawn=D(2021, 7, 1))
    assert advisory_trigger([adv], RELEASES, 90, FREEZE) is None


def test_earliest_advisory_wins() -> None:
    a1 = Advisory("A1", D(2022, 6, 1))
    a2 = Advisory("A2", D(2021, 6, 1))
    t = advisory_trigger([a1, a2], RELEASES, 90, FREEZE)
    assert t is not None and t.detail == "A2"


# ---- (c) -------------------------------------------------------------------------------
def test_wheel_filename_parsing() -> None:
    assert parse_wheel_filename("numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl") == WheelTag(
        "cp312", "cp312"
    )
    assert parse_wheel_filename("six-1.16.0-py2.py3-none-any.whl") == WheelTag("py2.py3", "none")
    assert parse_wheel_filename("pkg-1.0.tar.gz") is None


def test_pure_python_without_cap_is_supported() -> None:
    releases = [rel("1.0", D(2019, 1, 1), wheels=PURE, requires_python=">=3.6")]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_requires_python_cap_fires_at_release_plus_lag() -> None:
    releases = [rel("1.0", D(2020, 1, 1), wheels=PURE, requires_python=">=3.6,<3.11")]
    t = python_support_trigger(releases, PY, 12, FREEZE)
    assert t == TriggerEvent("python_support", D(2023, 10, 24), "requires_python excludes 3.11")


def test_cap_lifted_within_lag_is_fine() -> None:
    releases = [
        rel("1.0", D(2020, 1, 1), wheels=PURE, requires_python=">=3.6,<3.11"),
        rel("1.1", D(2023, 3, 1), wheels=PURE, requires_python=">=3.7"),
    ]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_compiled_package_missing_new_wheel_fires() -> None:
    wheels = (WheelTag("cp39", "cp39"), WheelTag("cp310", "cp310"))
    releases = [rel("2.0", D(2021, 11, 1), wheels=wheels)]
    t = python_support_trigger(releases, PY, 12, FREEZE)
    assert t is not None and t.detail == "no 3.11 wheel" and t.day == D(2023, 10, 24)


def test_compiled_package_with_new_wheel_ok_until_next() -> None:
    wheels = (WheelTag("cp310", "cp310"), WheelTag("cp311", "cp311"))
    releases = [rel("2.0", D(2022, 11, 1), wheels=wheels)]
    t = python_support_trigger(releases, PY, 12, FREEZE)
    assert t is not None and t.detail == "no 3.12 wheel"


def test_abi3_wheel_supports_later_versions() -> None:
    releases = [rel("2.0", D(2021, 1, 1), wheels=(WheelTag("cp37", "abi3"),))]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_sdist_only_is_supported_conservatively() -> None:
    releases = [rel("1.0", D(2019, 1, 1), classifiers=("Programming Language :: Python :: 3",))]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_python2_only_package_without_py3_evidence_is_skipped() -> None:
    releases = [rel("1.0", D(2019, 1, 1), requires_python="==2.7.*")]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_package_born_after_judgement_is_skipped() -> None:
    releases = [rel("1.0", D(2025, 1, 1), wheels=(WheelTag("cp313", "cp313"),))]
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


# ---- (d) and signals ------------------------------------------------------------------
def test_statement_trigger_is_phase1() -> None:
    with pytest.raises(NotImplementedError):
        statement_trigger("This project is no longer maintained.", [])


def test_inactive_classifier_is_a_signal_not_a_failure() -> None:
    releases = [
        rel("1.0", D(2018, 1, 1)),
        rel("1.1", D(2020, 4, 1), classifiers=(INACTIVE_CLASSIFIER,)),
    ]
    assert first_classifier_day(releases, INACTIVE_CLASSIFIER) == D(2020, 4, 1)
    assert archived_trigger(False, None, None, FREEZE) is None
    assert python_support_trigger(releases, PY, 12, FREEZE) is None


def test_first_failure_ignores_undated() -> None:
    a = TriggerEvent("archived", None, "undated")
    b = TriggerEvent("advisory", D(2022, 1, 1), "x")
    c = TriggerEvent("python_support", D(2021, 1, 1), "y")
    assert first_failure([a, b, None, c]) == c
    assert first_failure([a]) is None
