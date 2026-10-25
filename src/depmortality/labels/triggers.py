"""Failure triggers (CLAUDE.md section 3) and candidate signals.

Each trigger returns the date it becomes *knowable*, which is also the failure date:
(a) archive date; (b) advisory published date + window; (c) CPython X.Y.0 date + lag.
A trigger whose date is unknown is returned with ``day=None``: it is counted in reports
but cannot be placed on the timeline.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from depmortality.utils.months import add_months

INACTIVE_CLASSIFIER = "Development Status :: 7 - Inactive"
MATURE_CLASSIFIER = "Development Status :: 6 - Mature"
PY3_CLASSIFIER_PREFIX = "Programming Language :: Python :: 3"
FAILING_PYPI_STATUSES = frozenset({"archived", "deprecated"})


@dataclass(frozen=True)
class TriggerEvent:
    kind: str  # "archived" | "advisory" | "python_support" | "statement"
    day: dt.date | None
    detail: str


@dataclass(frozen=True)
class WheelTag:
    python: str  # e.g. "cp311", "py3", "py2.py3"
    abi: str  # e.g. "cp311", "abi3", "none"


@dataclass(frozen=True)
class Release:
    version: str
    upload_day: dt.date
    yanked: bool = False
    requires_python: str | None = None
    wheels: tuple[WheelTag, ...] = ()
    classifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Advisory:
    vuln_id: str
    published: dt.date
    withdrawn: dt.date | None = None
    fixed: tuple[str, ...] = ()
    last_affected: tuple[str, ...] = ()
    affected_versions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = field(default=())


def _version(text: str) -> Version | None:
    try:
        return Version(text)
    except InvalidVersion:
        return None


def parse_wheel_filename(filename: str) -> WheelTag | None:
    """``name-ver(-build)?-python-abi-platform.whl`` -> its python and abi tags."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    return WheelTag(python=parts[-3].lower(), abi=parts[-2].lower())


# ---- (a) archived / deprecated ---------------------------------------------------------
def archived_trigger(
    github_archived: bool,
    github_archived_at: dt.date | None,
    pypi_status: str | None,
    freeze: dt.date,
) -> TriggerEvent | None:
    if github_archived:
        if github_archived_at is None:
            return TriggerEvent("archived", None, "github archived; date unknown")
        if github_archived_at <= freeze:
            return TriggerEvent("archived", github_archived_at, "github archived")
    status = (pypi_status or "").strip().lower()
    if status in FAILING_PYPI_STATUSES:
        # PEP 792 status markers carry no date in the PyPI APIs we read.
        return TriggerEvent("archived", None, f"pypi status {status}; date unknown")
    return None


# ---- (b) unpatched advisory --------------------------------------------------------------
def _fix_release_day(adv: Advisory, releases: Sequence[Release]) -> dt.date | None:
    """Earliest upload of a non-yanked release that is outside the affected set."""
    parsed = [(v, r) for r in releases if not r.yanked and (v := _version(r.version))]
    fixed = [v for f in adv.fixed if (v := _version(f))]
    last = [v for f in adv.last_affected if (v := _version(f))]
    listed = [v for f in adv.affected_versions if (v := _version(f))]
    if fixed:
        threshold = min(fixed)
        candidates = [r.upload_day for v, r in parsed if v >= threshold]
    elif last:
        threshold = max(last)
        candidates = [r.upload_day for v, r in parsed if v > threshold]
    elif listed:
        threshold = max(listed)
        candidates = [r.upload_day for v, r in parsed if v > threshold]
    else:
        return None
    return min(candidates) if candidates else None


def advisory_trigger(
    advisories: Iterable[Advisory],
    releases: Sequence[Release],
    window_days: int,
    freeze: dt.date,
) -> TriggerEvent | None:
    """First advisory with no fixed release within the window (censored near the freeze)."""
    best: TriggerEvent | None = None
    for adv in advisories:
        deadline = adv.published + dt.timedelta(days=window_days)
        if adv.withdrawn is not None and adv.withdrawn <= deadline:
            continue
        if deadline > freeze:
            continue  # not yet determinable at the freeze: censored
        fix_day = _fix_release_day(adv, releases)
        if fix_day is not None and fix_day <= deadline:
            continue
        if best is None or best.day is None or deadline < best.day:
            best = TriggerEvent("advisory", deadline, adv.vuln_id)
    return best


# ---- (c) newest CPython not supported -----------------------------------------------------
def _cp_number(tag: str) -> tuple[int, int] | None:
    """'cp311' -> (3, 11); 'cp39' -> (3, 9). Anything else -> None."""
    if not tag.startswith("cp") or not tag[2:].isdigit() or len(tag) < 4:
        return None
    digits = tag[2:]
    return int(digits[0]), int(digits[1:])


def _requires_python_excludes(spec: str | None, major: int, minor: int) -> bool:
    """True if the spec rules out X.Y and the next two minors (an upper cap, not a floor)."""
    if not spec:
        return False
    try:
        s = SpecifierSet(spec)
    except InvalidSpecifier:
        return False
    return not any(s.contains(Version(f"{major}.{minor + k}.0")) for k in range(3))


def _py3_evidence(r: Release) -> bool:
    if r.requires_python:
        try:
            s = SpecifierSet(r.requires_python)
            if any(s.contains(Version(f"3.{k}.0")) for k in range(0, 20)):
                return True
        except InvalidSpecifier:
            pass
    for w in r.wheels:
        if "py3" in w.python.split(".") or (_cp_number(w.python) or (0, 0))[0] == 3:
            return True
    return any(c.startswith(PY3_CLASSIFIER_PREFIX) for c in r.classifiers)


def _supports(r: Release, major: int, minor: int) -> bool:
    for w in r.wheels:
        if w.abi == "none" and any(p in ("py3", f"py{major}{minor}") for p in w.python.split(".")):
            return True
        cp = _cp_number(w.python)
        if cp == (major, minor):
            return True
        if w.abi == "abi3" and cp is not None and cp[0] == major and cp[1] <= minor:
            return True
    return False


def _is_compiled(r: Release) -> bool:
    return any(_cp_number(w.python) is not None and w.abi not in ("abi3", "none") for w in r.wheels)


def python_support_trigger(
    releases: Sequence[Release],
    python_releases: dict[str, dt.date],
    lag_months: int,
    freeze: dt.date,
    *,
    require_py3_evidence: bool = True,
) -> TriggerEvent | None:
    """Fires only on positive evidence of non-support at X.Y.0 + lag (DECISIONS.md)."""
    live = sorted(
        (r for r in releases if not r.yanked),
        key=lambda r: (r.upload_day, _version(r.version) or Version("0")),
    )
    for label, released in sorted(python_releases.items(), key=lambda kv: kv[1]):
        major, minor = (int(x) for x in label.split("."))
        judged = add_months(released, lag_months)
        if judged > freeze:
            break
        known = [r for r in live if r.upload_day <= judged]
        if not known:
            continue
        if require_py3_evidence and not any(_py3_evidence(r) for r in known):
            continue
        latest = known[-1]
        if _requires_python_excludes(latest.requires_python, major, minor):
            return TriggerEvent("python_support", judged, f"requires_python excludes {label}")
        with_wheels = [r for r in known if r.wheels]
        compiled = bool(with_wheels) and _is_compiled(with_wheels[-1])
        if compiled and not any(_supports(r, major, minor) for r in known):
            return TriggerEvent("python_support", judged, f"no {label} wheel")
    return None


# ---- (d) explicit statement (Phase 1) -----------------------------------------------------
def statement_trigger(
    readme: str | None, maintainer_comments: Sequence[str]
) -> TriggerEvent | None:
    raise NotImplementedError("trigger (d), the LLM statement extractor, is Phase 1 scope")


# ---- candidate signals (recorded, never failures by themselves) --------------------------
def first_classifier_day(releases: Sequence[Release], classifier: str) -> dt.date | None:
    days = [r.upload_day for r in releases if classifier in r.classifiers]
    return min(days) if days else None


def first_failure(events: Iterable[TriggerEvent | None]) -> TriggerEvent | None:
    dated = [e for e in events if e is not None and e.day is not None]
    return min(dated, key=lambda e: (e.day, e.kind)) if dated else None
