"""End-to-end on synthetic interim tables: build -> processed -> hand-check CSV -> Gate 1.

The synthetic packages are test fixtures, not data; nothing here feeds a report.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from depmortality.build import Interim, assert_no_identities, build
from depmortality.config import Settings
from depmortality.reports import gate1, handcheck
from depmortality.utils.paths import DataPaths

D = dt.date
SALT = "test-salt-0123456789"


def _interim() -> Interim:
    pilot = pl.DataFrame(
        {
            "name": ["alive", "archived-pkg", "quietpkg", "noclone"],
            "repo": ["jdoe/alive", "acme/archived-pkg", "solo/quietpkg", "gone/noclone"],
            "direct_dependents": [500, 40, 3, 2],
            "first_upload": [D(2014, 1, 5), D(2015, 6, 1), D(2016, 2, 1), D(2017, 1, 1)],
            "packages_in_repo": [1, 1, 1, 1],
            "stratum": ["top", "top", "2-4|2016", "2-4|2017"],
            "weight": [1.0, 1.0, 10.0, 10.0],
            "selected_as": ["top", "top", "tail", "tail"],
        }
    )

    def rel(
        pkg: str, v: str, day: dt.date, wheels: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        return {
            "package": pkg,
            "version": v,
            "upload_day": day,
            "yanked": False,
            "requires_python": ">=3.6",
            "wheels": wheels or [{"python": "py3", "abi": "none"}],
        }

    releases = pl.DataFrame(
        [
            rel("alive", "1.0", D(2014, 1, 5)),
            rel("alive", "2.0", D(2024, 5, 1)),
            rel("archived-pkg", "0.1", D(2015, 6, 1)),
            rel("quietpkg", "0.1", D(2016, 2, 1)),
            rel("noclone", "0.1", D(2017, 1, 1)),
        ],
        schema={
            "package": pl.String,
            "version": pl.String,
            "upload_day": pl.Date,
            "yanked": pl.Boolean,
            "requires_python": pl.String,
            "wheels": pl.List(pl.Struct({"python": pl.String, "abi": pl.String})),
        },
    )
    classifiers = pl.DataFrame(
        {
            "package": ["quietpkg"],
            "version": ["0.1"],
            "classifiers": [["Development Status :: 7 - Inactive"]],
        }
    )
    meta = pl.DataFrame(
        {
            "package": ["alive", "archived-pkg", "quietpkg", "noclone"],
            "status": [None, None, None, None],
            "roles": [["jdoe"], ["acme-dev"], ["solo"], []],
            "home_page": [None] * 4,
            "n_releases": [2, 1, 1, 1],
        },
        schema={
            "package": pl.String,
            "status": pl.String,
            "roles": pl.List(pl.String),
            "home_page": pl.String,
            "n_releases": pl.Int64,
        },
    )

    def commit(
        pkg: str, repo: str, day: dt.date, name: str, email: str, sha: str
    ) -> dict[str, object]:
        return {
            "package": pkg,
            "repo": repo,
            "sha": sha,
            "n_parents": 1,
            "author_name": name,
            "author_email": email,
            "author_day": day,
            "committer_name": name,
            "committer_email": email,
            "committer_day": day,
        }

    commits = pl.DataFrame(
        [
            commit(
                "alive",
                "jdoe/alive",
                D(2014 + i // 4, 1 + (i % 4) * 3, 2),
                "Jane Doe",
                "jane@uni.edu",
                f"{i:040x}",
            )
            for i in range(48)
        ]
        + [
            commit(
                "archived-pkg", "acme/archived-pkg", D(2015, 7, 1), "Dev", "dev@acme.io", "a" * 40
            ),
            commit("quietpkg", "solo/quietpkg", D(2016, 3, 1), "Solo", "solo@mail.org", "b" * 40),
            commit(
                "quietpkg",
                "solo/quietpkg",
                D(2019, 3, 1),
                "dependabot[bot]",
                "49699333+dependabot[bot]@users.noreply.github.com",
                "c" * 40,
            ),
        ]
    )
    logins = pl.DataFrame({"repo": ["jdoe/alive"], "email": ["jane@uni.edu"], "login": ["jdoe"]})
    gh = pl.DataFrame(
        {
            "repo": ["jdoe/alive", "acme/archived-pkg", "solo/quietpkg", "gone/noclone"],
            "found": [True, True, True, False],
            "database_id": [1, 2, 3, None],
            "name_with_owner": ["jdoe/alive", "acme/archived-pkg", "solo/quietpkg", None],
            "is_archived": [False, True, False, False],
            "archived_at": [None, D(2018, 4, 2), None, None],
            "created_at": [D(2013, 1, 1), D(2015, 1, 1), D(2016, 1, 1), None],
            "pushed_at": [None] * 4,
            "is_fork": [False] * 4,
            "is_mirror": [False] * 4,
            "default_branch": ["main", "master", "master", None],
            "owner_type": ["User", "Organization", "User", None],
            "owner_login": ["jdoe", "acme", "solo", None],
        },
        schema_overrides={
            "database_id": pl.Int64,
            "archived_at": pl.Date,
            "pushed_at": pl.Date,
            "created_at": pl.Date,
        },
    )
    advisories = pl.DataFrame(
        schema={
            "package": pl.String,
            "vuln_id": pl.String,
            "published": pl.Date,
            "withdrawn": pl.Date,
            "fixed": pl.List(pl.String),
            "last_affected": pl.List(pl.String),
            "affected_versions": pl.List(pl.String),
        }
    )
    gha = pl.DataFrame(
        {
            "package": ["alive", "alive"],
            "repo": ["jdoe/alive"] * 2,
            "day": [D(2020, 5, 1), D(2020, 5, 2)],
            "source": ["gha"] * 2,
            "kind": ["issue_reply", "issue_reply"],
            "actor_login": ["jdoe", "renovate[bot]"],
            "actor_name": [None, None],
            "actor_email": [None, None],
            "author_association": ["OWNER", "NONE"],
        },
        schema_overrides={"actor_name": pl.String, "actor_email": pl.String},
    )
    return Interim(pilot, releases, classifiers, meta, commits, logins, gh, advisories, gha)


@pytest.fixture()
def built(tmp_path: Path, settings: Settings) -> DataPaths:
    paths = DataPaths(tmp_path / "data").ensure()
    interim = _interim()
    for name in Interim.__dataclass_fields__:
        getattr(interim, name).write_parquet(paths.interim / f"{name}.parquet")
    pl.DataFrame(
        {
            "package": ["noclone"],
            "step": ["git"],
            "ok": [False],
            "reason": ["CloneFailed: repository not found"],
        }
    ).write_parquet(paths.interim / "pull_status.parquet")
    (paths.interim / "pilot_meta.json").write_text(json.dumps({"eligible_packages": 22}))
    (paths.interim / "universe_meta.json").write_text(
        json.dumps({"packages": 25, "depsdev_snapshot": "synthetic"})
    )
    build(Interim.read(paths.interim), settings, SALT).write(paths.processed)
    return paths


def test_processed_tables_are_identity_free(built: DataPaths) -> None:
    for f in built.processed.glob("*.parquet"):
        frame = pl.read_parquet(f)
        assert_no_identities(frame, f.stem)
        text = frame.write_csv()
        assert "jane@uni.edu" not in text and "Jane Doe" not in text


def test_states_and_triggers_on_synthetic_packages(built: DataPaths) -> None:
    packages = pl.read_parquet(built.processed / "packages.parquet")
    assert sorted(packages["package"]) == ["alive", "archived-pkg", "quietpkg"]  # noclone excluded
    tr = pl.read_parquet(built.processed / "transitions.parquet")
    arch = tr.filter(pl.col("package") == "archived-pkg")
    assert arch.filter(pl.col("to_state") == "FAILED")["trigger"].to_list() == ["archived"]
    quiet = tr.filter(pl.col("package") == "quietpkg")
    # The 2019 dependabot commit must not revive it.
    assert quiet["to_state"].to_list() == ["QUIET"]
    signals = pl.read_parquet(built.processed / "signals.parquet")
    assert signals.filter(pl.col("package") == "quietpkg")["inactive_classifier_day"].item() == D(
        2016, 2, 1
    )


def test_reports_render(built: DataPaths, settings: Settings, tmp_path: Path) -> None:
    out = tmp_path / "reports"
    csv_path = handcheck.write(built, settings, out)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 3
    assert all(r["verdict_states_ok"] == "" for r in rows)
    quiet_row = next(r for r in rows if r["package"] == "quietpkg")
    assert "dependabot[bot]" in quiet_row["bot_actors_removed"]
    assert "@" not in "".join(r["bot_actors_removed"] + r["activity_by_month"] for r in rows)
    md = gate1.write(built, settings, out).read_text(encoding="utf-8")
    assert "## Verdict" in md and "provisional" in md
    assert (out / "figures" / "gate1_km_time_to_quiet.png").exists()
    assert "CloneFailed" in md


def test_chunked_build_matches_single_chunk(
    built: DataPaths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import depmortality.build as build_module

    whole = build(Interim.read(built.interim), settings, SALT)
    monkeypatch.setattr(build_module, "BUILD_CHUNK", 1)
    chunked = build(Interim.read(built.interim), settings, SALT)
    for name in whole.__dataclass_fields__:
        a, b = getattr(whole, name), getattr(chunked, name)
        assert a.sort(a.columns).equals(b.sort(b.columns)), name
