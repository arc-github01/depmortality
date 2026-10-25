"""Parsers on trimmed real responses saved under tests/fixtures (identities replaced)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from depmortality.config import PilotConfig
from depmortality.data import depsdev, gharchive
from depmortality.data.github import (
    commit_login_query,
    parse_commit_logins,
    parse_repo_response,
    repo_query,
)
from depmortality.data.gitlog import parse_log
from depmortality.data.osv import parse_vuln
from depmortality.data.pypi import parse_project_json, parse_project_status, parse_roles
from depmortality.labels.triggers import WheelTag, parse_wheel_filename

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_pypi_json_six() -> None:
    info = parse_project_json("six", json.loads((FIX / "pypi_six.json").read_text()))
    versions = [r.version for r in info.releases]
    assert versions == ["1.9.0", "1.10.0", "1.16.0", "1.17.0"]  # chronological, not lexical
    last = info.releases[-1]
    assert last.upload_day == dt.date(2024, 12, 4)
    assert WheelTag("py2.py3", "none") in last.wheels
    assert last.requires_python is not None and ">=2.7" in last.requires_python


def test_simple_api_status_and_missing_status() -> None:
    assert parse_project_status(json.loads((FIX / "pypi_simple_six.json").read_text())) == "active"
    assert parse_project_status({"name": "x"}) is None
    assert parse_project_status({"project-status": {"status": "Archived"}}) == "archived"


def test_roles_xml() -> None:
    roles = parse_roles((FIX / "pypi_roles_numpy.xml").read_bytes())
    assert roles and all(role in {"Owner", "Maintainer"} for role, _ in roles)


def test_numpy_wheels_are_cp_specific() -> None:
    tags = {
        parse_wheel_filename(n) for n in (FIX / "numpy_wheel_filenames.txt").read_text().split()
    }
    tags.discard(None)
    assert any(
        t is not None and t.python.startswith("cp3") and t.abi.startswith("cp3") for t in tags
    )


def test_osv_vuln_parses_ecosystem_range() -> None:
    payload = json.loads((FIX / "osv_GHSA-22hg-r758-rrw9.json").read_text())
    advs = parse_vuln(payload, "Pillow")
    assert len(advs) == 1
    assert advs[0].fixed == ("13.0.0",)
    assert advs[0].published == dt.date(2026, 10, 21)
    assert parse_vuln(payload, "numpy") == []


def test_git_log_parse() -> None:
    commits = parse_log((FIX / "git_six_head.log").read_text(encoding="utf-8"))
    assert len(commits) == 3
    assert all(len(c.sha) == 40 for c in commits)
    assert commits[0].committer_day >= commits[-1].committer_day
    assert commits[0].author_email == "author0@example.org"


def test_github_repo_query_and_parse() -> None:
    q = repo_query(["benjaminp/six", "pallets/flask"])
    assert 'r1: repository(owner: "pallets", name: "flask")' in q
    with pytest.raises(ValueError):
        repo_query(['evil") { x } #/y'])
    payload = {
        "data": {
            "r0": {
                "databaseId": 1,
                "nameWithOwner": "benjaminp/six",
                "isArchived": False,
                "archivedAt": None,
                "createdAt": "2013-12-01T00:00:00Z",
                "pushedAt": None,
                "isFork": False,
                "isMirror": False,
                "defaultBranchRef": {"name": "main"},
                "owner": {"__typename": "User", "login": "benjaminp"},
            },
            "r1": None,
        }
    }
    a, b = parse_repo_response(["benjaminp/six", "gone/repo"], payload)
    assert a.found and a.default_branch == "main" and a.owner_type == "User"
    assert not b.found


def test_commit_login_query_and_parse() -> None:
    sha = "a" * 40
    assert f'object(oid: "{sha}")' in commit_login_query("o/r", [sha])
    with pytest.raises(ValueError):
        commit_login_query("o/r", ["not-a-sha"])
    payload = {
        "data": {
            "repository": {
                "c0": {"author": {"email": "x@y.org", "user": {"login": "jdoe"}}},
                "c1": {"author": {"email": "z@y.org", "user": None}},
            }
        }
    }
    assert parse_commit_logins([sha, sha], payload) == [("x@y.org", "jdoe")]


def test_github_repo_from_project_name() -> None:
    assert depsdev.github_repo("github.com/Benjaminp/Six") == "benjaminp/six"
    assert depsdev.github_repo("github.com/a/b.git") == "a/b"
    assert depsdev.github_repo("factset/enterprise-sdk") == "factset/enterprise-sdk"
    assert depsdev.github_repo("gitlab.com/a/b") is None
    assert depsdev.normalise_name("Zope.Interface") == "zope-interface"


def test_gharchive_sql_prunes_by_literal_year() -> None:
    sql = gharchive.extraction_sql(2015, dt.date(2026, 10, 31))
    assert "BETWEEN '201501' AND '201512'" in sql and "payload" in sql
    last = gharchive.extraction_sql(2026, dt.date(2026, 10, 31))
    assert "AND '202610'" in last
    with pytest.raises(ValueError):
        gharchive.extraction_sql(2027, dt.date(2026, 10, 31))


def test_gharchive_rows_to_events() -> None:
    raw = pl.DataFrame(
        {
            "event_id": ["1", "2", "3", "3", "4"],
            "type": [
                "IssueCommentEvent",
                "IssueCommentEvent",
                "IssuesEvent",
                "IssuesEvent",
                "IssueCommentEvent",
            ],
            "created_at": [dt.datetime(2020, 1, 1, 12)] * 5,
            "repo_id": [7, 7, 7, 7, 7],
            "repo_name": ["old/name"] * 5,
            "actor_id": [1, 2, 3, 3, 1],
            "actor_login": ["jdoe", "user", "user", "user", "jdoe"],
            "action": ["created", "created", "opened", "opened", "edited"],
            "author_association": ["OWNER", "NONE", "NONE", "NONE", "OWNER"],
            "on_pull_request": [True, False, False, False, False],
            "ref": [None] * 5,
        }
    )
    events = gharchive.to_events(raw, {"new/name": ["pkg"]}, {7: "new/name"})
    assert sorted(events["kind"].to_list()) == [
        "issue_opened",
        "issue_reply",
        "pr_reply",
    ]  # dup + edit dropped
    assert set(events["repo"].to_list()) == {"new/name"}


def _universe(n: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "name": [f"p{i:04d}" for i in range(n)],
            "repo": [f"o/p{i}" for i in range(n)],
            "direct_dependents": [1 + (i * 37) % 300 for i in range(n)],
            "first_upload": [dt.date(2012 + i % 11, 1, 1) for i in range(n)],
            "packages_in_repo": [1] * n,
        }
    )


def _cfg(settings_pilot: PilotConfig, **kw: object) -> PilotConfig:
    return settings_pilot.model_copy(update=kw)


def test_pilot_selection_is_seeded_stratified_and_weighted(settings: object) -> None:
    from depmortality.config import Settings

    assert isinstance(settings, Settings)
    cfg = _cfg(settings.pilot, n_top=50, n_tail=200)
    uni = _universe(3000)
    a = depsdev.select_pilot(uni, cfg)
    b = depsdev.select_pilot(uni, cfg)
    assert a.equals(b)
    assert a.filter(pl.col("selected_as") == "top").height == 50
    assert a.filter(pl.col("selected_as") == "tail").height == 200
    top_min = a.filter(pl.col("selected_as") == "top")["direct_dependents"].min()
    tail_max = uni.sort("direct_dependents", descending=True).slice(50)["direct_dependents"].max()
    assert top_min >= tail_max  # type: ignore[operator]
    # Weights reproduce the tail population size.
    tail = a.filter(pl.col("selected_as") == "tail")
    assert tail["weight"].sum() == pytest.approx(3000 - 50)
    other = depsdev.select_pilot(uni, _cfg(cfg, seed=cfg.seed + 1))
    assert not other.equals(a)
