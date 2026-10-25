from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from depmortality.data import gharchive
from depmortality.data.gha_mirror import ASSOCIATION_WINDOW, query_sql, to_slim


def raw(rows: list[tuple[str, str, dt.datetime, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "type": [r[0] for r in rows],
            "actor_login": [r[1] for r in rows],
            "repo_name": ["Numpy/numpy"] * len(rows),
            "created_at": [r[2] for r in rows],
            "action": [r[3] for r in rows],
            "number": [1] * len(rows),
            "comment_id": list(range(len(rows))),
            "author_association": [r[4] for r in rows],
            "ref": [""] * len(rows),
        }
    )


def test_association_outside_window_is_null_not_none() -> None:
    lo, hi = ASSOCIATION_WINDOW
    frame = to_slim(
        raw(
            [
                ("IssueCommentEvent", "a", dt.datetime(2015, 5, 1), "created", "NONE"),
                ("IssueCommentEvent", "b", dt.datetime(lo.year, lo.month, 2), "created", "NONE"),
                ("IssueCommentEvent", "c", dt.datetime(2020, 1, 1), "created", "OWNER"),
                ("IssueCommentEvent", "d", dt.datetime(hi.year + 1, 1, 1), "created", "NONE"),
            ]
        )
    )
    assert frame["author_association"].to_list() == [None, "NONE", "OWNER", None]
    assert frame["repo_name"].unique().to_list() == ["numpy/numpy"]


def test_action_none_becomes_null_and_duplicates_drop() -> None:
    row = ("PushEvent", "a", dt.datetime(2020, 1, 1), "none", "NONE")
    frame = to_slim(raw([row, row]).with_columns(pl.lit(7).alias("comment_id")))
    assert frame.height == 1
    assert frame["action"].to_list() == [None]


def test_mirror_rows_flow_into_events() -> None:
    frame = to_slim(
        raw(
            [
                ("IssueCommentEvent", "maint", dt.datetime(2020, 1, 1), "created", "MEMBER"),
                ("IssueCommentEvent", "maint", dt.datetime(2020, 1, 2), "edited", "MEMBER"),
                ("IssuesEvent", "user", dt.datetime(2020, 1, 3), "opened", "NONE"),
            ]
        )
    )
    events = gharchive.to_events(frame, {"numpy/numpy": ["numpy"]}, {})
    assert sorted(events["kind"].to_list()) == ["issue_opened", "issue_reply"]


def test_query_rejects_unsafe_names_and_bounds_dates() -> None:
    sql = query_sql(["numpy/numpy"], dt.date(2011, 1, 1), dt.date(2025, 9, 30))
    assert "created_at < '2025-10-01 00:00:00'" in sql and "'numpy/numpy'" in sql
    with pytest.raises(ValueError):
        query_sql(["x/y') OR 1=1 --"], dt.date(2011, 1, 1), dt.date(2025, 9, 30))
