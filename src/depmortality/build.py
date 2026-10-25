"""Interim tables -> processed analysis tables (panel, states, transitions, triggers).

Interim tables may contain emails and logins; processed tables contain only salted person
hashes. ``assert_no_identities`` enforces that on every processed table before writing.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from depmortality.config import Settings
from depmortality.labels.bots import BotFilter
from depmortality.labels.identity import IdentityResolver, email_key, hash_person, login_key
from depmortality.labels.panel import (
    EVENT_SCHEMA,
    build_panel,
    flag_bots,
    flag_maintainer_activity,
)
from depmortality.labels.states import Failure
from depmortality.labels.timeline import states_from_panel
from depmortality.labels.triggers import (
    INACTIVE_CLASSIFIER,
    MATURE_CLASSIFIER,
    Advisory,
    Release,
    TriggerEvent,
    WheelTag,
    advisory_trigger,
    archived_trigger,
    first_classifier_day,
    first_failure,
    python_support_trigger,
)
from depmortality.utils.months import month_index

log = logging.getLogger(__name__)

EMAIL_LIKE = r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}"


@dataclass(frozen=True)
class Interim:
    pilot: pl.DataFrame
    releases: pl.DataFrame
    release_classifiers: pl.DataFrame
    pypi_meta: pl.DataFrame
    commits: pl.DataFrame
    commit_logins: pl.DataFrame
    github_repos: pl.DataFrame
    advisories: pl.DataFrame
    gha_events: pl.DataFrame

    @classmethod
    def read(cls, root: Path) -> Interim:
        return cls(
            **{name: pl.read_parquet(root / f"{name}.parquet") for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True)
class Processed:
    packages: pl.DataFrame
    panel: pl.DataFrame
    states: pl.DataFrame
    transitions: pl.DataFrame
    triggers: pl.DataFrame
    signals: pl.DataFrame

    def write(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for name in self.__dataclass_fields__:
            frame: pl.DataFrame = getattr(self, name)
            assert_no_identities(frame, name)
            frame.write_parquet(root / f"{name}.parquet")


def assert_no_identities(frame: pl.DataFrame, name: str) -> None:
    """No email-like strings in any string column of a processed table."""
    for col, dtype in frame.schema.items():
        if dtype == pl.String:
            hits = frame.filter(pl.col(col).str.contains(EMAIL_LIKE)).height
            if hits:
                raise ValueError(f"processed table {name}.{col} contains {hits} email-like values")
        if col in {"actor_login", "actor_email", "actor_name", "author_email", "committer_email"}:
            raise ValueError(f"processed table {name} has identity column {col}")


# ---- events -------------------------------------------------------------------------------
def commit_events(commits: pl.DataFrame, logins: pl.DataFrame) -> pl.DataFrame:
    mapped = logins.select(
        "repo", pl.col("email").str.to_lowercase().alias("_email"), pl.col("login").alias("_login")
    ).unique(subset=["repo", "_email"], keep="first")
    return (
        commits.with_columns(pl.col("author_email").str.to_lowercase().alias("_email"))
        .join(mapped, on=["repo", "_email"], how="left")
        .select(
            "package",
            "repo",
            pl.col("committer_day").alias("day"),
            pl.lit("git").alias("source"),
            pl.lit("commit").alias("kind"),
            pl.col("_login").alias("actor_login"),
            pl.col("author_name").alias("actor_name"),
            pl.col("author_email").alias("actor_email"),
            pl.lit(None, dtype=pl.String).alias("author_association"),
        )
    )


def release_events(releases: pl.DataFrame, pilot: pl.DataFrame) -> pl.DataFrame:
    return releases.join(
        pilot.select(pl.col("name").alias("package"), "repo"), on="package"
    ).select(
        "package",
        "repo",
        pl.col("upload_day").alias("day"),
        pl.lit("pypi").alias("source"),
        pl.lit("release").alias("kind"),
        *[
            pl.lit(None, dtype=pl.String).alias(c)
            for c in ("actor_login", "actor_name", "actor_email", "author_association")
        ],
    )


def resolve_people(events: pl.DataFrame, interim: Interim, salt: str) -> pl.DataFrame:
    """Adds the hashed ``person`` column. Merging rules: labels/identity.py."""
    r = IdentityResolver()
    for repo, email in (
        events.filter(pl.col("actor_email").is_not_null())
        .select("repo", "actor_email")
        .unique()
        .iter_rows()
    ):
        r.add_email(email, repo)
    for repo, email, login in interim.commit_logins.select("repo", "email", "login").iter_rows():
        r.link_email_login(email, login, repo)
    repo_logins = (
        events.filter(pl.col("actor_login").is_not_null())
        .group_by("package")
        .agg(pl.col("actor_login").unique())
    )
    roles = interim.pypi_meta.select("package", "roles")
    for _package, accounts, logins in roles.join(repo_logins, on="package", how="left").iter_rows():
        r.link_pypi_accounts(accounts or [], logins or [])

    def person(row: tuple[str, str | None, str | None]) -> str | None:
        repo, login, email = row
        if login:
            return hash_person(r.person(login_key(login)), salt)
        if email:
            return hash_person(r.person(email_key(email, repo)), salt)
        return None

    keys = events.select("repo", "actor_login", "actor_email").unique()
    hashed = [person(row) for row in keys.iter_rows()]
    keyed = keys.with_columns(pl.Series("person", hashed, dtype=pl.String))
    return events.join(
        keyed, on=["repo", "actor_login", "actor_email"], how="left", nulls_equal=True
    )


# ---- triggers -----------------------------------------------------------------------------
def _releases_for(package: str, interim: Interim) -> list[Release]:
    rel = interim.releases.filter(pl.col("package") == package)
    cls = {
        v: tuple(c or [])
        for v, c in interim.release_classifiers.filter(pl.col("package") == package)
        .select("version", "classifiers")
        .iter_rows()
    }
    out = []
    for version, day, yanked, requires, wheels in rel.select(
        "version", "upload_day", "yanked", "requires_python", "wheels"
    ).iter_rows():
        out.append(
            Release(
                version=version,
                upload_day=day,
                yanked=bool(yanked),
                requires_python=requires,
                wheels=tuple(WheelTag(w["python"], w["abi"]) for w in wheels or []),
                classifiers=cls.get(version, ()),
            )
        )
    return out


def _advisories_for(package: str, interim: Interim) -> list[Advisory]:
    rows = interim.advisories.filter(pl.col("package") == package)
    return [
        Advisory(
            vuln_id=r["vuln_id"],
            published=r["published"],
            withdrawn=r["withdrawn"],
            fixed=tuple(r["fixed"] or ()),
            last_affected=tuple(r["last_affected"] or ()),
            affected_versions=tuple(r["affected_versions"] or ()),
        )
        for r in rows.iter_rows(named=True)
    ]


def package_triggers(
    package: str, repo: str, interim: Interim, settings: Settings
) -> tuple[list[TriggerEvent], dict[str, dt.date | None]]:
    freeze = settings.splits.data_freeze
    t = settings.thresholds.triggers
    releases = _releases_for(package, interim)
    gh = interim.github_repos.filter(pl.col("repo") == repo)
    meta = interim.pypi_meta.filter(pl.col("package") == package)
    archived = archived_trigger(
        bool(gh["is_archived"].item()) if gh.height else False,
        gh["archived_at"].item() if gh.height else None,
        meta["status"].item() if meta.height else None,
        freeze,
    )
    advisory = advisory_trigger(
        _advisories_for(package, interim), releases, t.advisory_window_days, freeze
    )
    python = python_support_trigger(
        releases,
        settings.python_releases.releases,
        t.python_support_lag_months,
        freeze,
        require_py3_evidence=t.python_support_requires_py3_evidence,
    )
    events = [e for e in (archived, advisory, python) if e is not None]
    signals = {
        "inactive_classifier_day": first_classifier_day(releases, INACTIVE_CLASSIFIER),
        "mature_classifier_day": first_classifier_day(releases, MATURE_CLASSIFIER),
    }
    return events, signals


# ---- main -----------------------------------------------------------------------------------
def build(interim: Interim, settings: Settings, salt: str) -> Processed:
    bots = BotFilter.from_config(settings.bots)
    freeze_month = month_index(settings.splits.data_freeze)
    window = settings.thresholds.states.quiet_window_months
    pilot = interim.pilot.rename({"name": "package"})

    events = pl.concat(
        [
            commit_events(interim.commits, interim.commit_logins),
            release_events(interim.releases, interim.pilot),
            interim.gha_events.select(*(c for c in EVENT_SCHEMA if c != "person")),
        ],
        how="vertical_relaxed",
    )
    events = resolve_people(events, interim, salt).select(list(EVENT_SCHEMA))
    events = flag_maintainer_activity(flag_bots(events, bots))

    origins = interim.releases.group_by("package").agg(
        pl.col("upload_day").min().alias("origin_day")
    )
    # A package without its git history would look falsely Quiet, so it is excluded (and
    # reported as a pull failure) rather than analysed with commits missing.
    with_git = interim.commits.select("package").unique()
    spans = (
        pilot.select("package")
        .join(with_git, on="package", how="semi")
        .join(origins, on="package", how="inner")
        .select(
            "package",
            (
                pl.col("origin_day").dt.year().cast(pl.Int32) * 12
                + pl.col("origin_day").dt.month().cast(pl.Int32)
                - 1
            ).alias("origin_month"),
            pl.lit(freeze_month, dtype=pl.Int32).alias("end_month"),
        )
        .filter(pl.col("origin_month") <= pl.col("end_month"))
    )
    panel = build_panel(events, spans)

    trig_rows: list[dict[str, object]] = []
    sig_rows: list[dict[str, object]] = []
    failures: dict[str, Failure] = {}
    for package, repo in (
        pilot.join(spans.select("package"), on="package").select("package", "repo").iter_rows()
    ):
        trig, sig = package_triggers(package, repo, interim, settings)
        for e in trig:
            trig_rows.append({"package": package, "kind": e.kind, "day": e.day, "detail": e.detail})
        sig_rows.append({"package": package, **sig})
        first = first_failure(trig)
        if first is not None and first.day is not None:
            failures[package] = Failure(month_index(first.day), first.kind)
    states, transitions = states_from_panel(panel, failures, window)

    post_failure = (
        events.filter(pl.col("is_maintainer_activity"))
        .with_columns(
            (
                pl.col("day").dt.year().cast(pl.Int32) * 12
                + pl.col("day").dt.month().cast(pl.Int32)
                - 1
            ).alias("month")
        )
        .join(
            pl.DataFrame(
                {"package": list(failures), "fail_month": [f.month for f in failures.values()]},
                schema={"package": pl.String, "fail_month": pl.Int32},
            ),
            on="package",
        )
        .filter((pl.col("month") > pl.col("fail_month")) & (pl.col("month") <= freeze_month))
        .group_by("package")
        .len("post_failure_activity")
    )
    gh = interim.github_repos.select("repo", "owner_type", "found", "is_fork", "created_at")
    packages = (
        pilot.join(spans, on="package", how="inner")
        .join(gh, on="repo", how="left")
        .join(post_failure, on="package", how="left")
        .with_columns(pl.col("post_failure_activity").fill_null(0))
        .select(
            "package",
            "repo",
            "stratum",
            "weight",
            "selected_as",
            "direct_dependents",
            "packages_in_repo",
            "origin_month",
            "end_month",
            "owner_type",
            "is_fork",
            pl.col("created_at").alias("repo_created_at"),
            "post_failure_activity",
        )
    )
    triggers = pl.DataFrame(
        trig_rows,
        schema={"package": pl.String, "kind": pl.String, "day": pl.Date, "detail": pl.String},
    )
    signals = pl.DataFrame(
        sig_rows,
        schema={
            "package": pl.String,
            "inactive_classifier_day": pl.Date,
            "mature_classifier_day": pl.Date,
        },
    )
    return Processed(packages, panel, states, transitions, triggers, signals)
