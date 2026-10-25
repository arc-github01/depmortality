"""Panel construction: bot removal first, months start at the first release."""

from __future__ import annotations

import datetime as dt

import polars as pl

from depmortality.labels.bots import BotFilter
from depmortality.labels.panel import (
    EVENT_SCHEMA,
    build_panel,
    flag_bots,
    flag_maintainer_activity,
)
from depmortality.labels.states import State
from depmortality.labels.timeline import states_from_panel
from depmortality.utils.months import month_index

PKG = "tinyretry"
REPO = "jdoe/tinyretry"


def ev(
    day: str, kind: str, login: str | None = None, *, source: str = "gha", **kw: str
) -> dict[str, object]:
    row: dict[str, object] = {k: None for k in EVENT_SCHEMA}
    row.update(
        package=PKG,
        repo=REPO,
        day=dt.date.fromisoformat(day),
        source=source,
        kind=kind,
        actor_login=login,
        person=f"p:{login}" if login else None,
    )
    row.update(kw)
    return row


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=EVENT_SCHEMA)


def spans(origin: str, end: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "package": [PKG],
            "origin_month": [month_index(dt.date.fromisoformat(origin))],
            "end_month": [month_index(dt.date.fromisoformat(end))],
        },
        schema={"package": pl.String, "origin_month": pl.Int32, "end_month": pl.Int32},
    )


def pipeline(rows: list[dict[str, object]], bots: BotFilter, origin: str, end: str) -> pl.DataFrame:
    events = flag_maintainer_activity(flag_bots(frame(rows), bots))
    return build_panel(events, spans(origin, end))


def test_months_start_at_first_release_and_pre_origin_events_dropped(bots: BotFilter) -> None:
    rows = [
        ev("2014-06-03", "commit", "jdoe", source="git"),  # before the first release
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2015-02-11", "commit", "jdoe", source="git"),
    ]
    panel = pipeline(rows, bots, "2015-01-10", "2015-04-30")
    assert panel["month"].to_list()[0] == month_index(dt.date(2015, 1, 1))
    assert panel.height == 4
    assert panel["n_commits"].sum() == 1
    assert panel["n_releases"].sum() == 1


def test_bot_activity_removed_before_counting(bots: BotFilter) -> None:
    rows = [
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2015-03-01", "commit", "dependabot[bot]", source="git"),
        ev(
            "2015-03-02",
            "commit",
            None,
            source="git",
            actor_name="pre-commit-ci[bot]",
            actor_email="66853113+pre-commit-ci[bot]@users.noreply.github.com",
        ),
        ev("2015-03-05", "issue_reply", "github-actions[bot]", author_association="NONE"),
    ]
    panel = pipeline(rows, bots, "2015-01-10", "2015-04-30")
    march = panel.filter(pl.col("month") == month_index(dt.date(2015, 3, 1)))
    assert march["n_maintainer_activity"].item() == 0
    assert march["n_bot_events"].item() == 3


def test_revival_by_bot_only_does_not_count(bots: BotFilter) -> None:
    rows = [
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2015-02-01", "commit", "jdoe", source="git"),
        # 3 years later, only bots touch the repo.
        ev("2018-02-01", "commit", "dependabot[bot]", source="git"),
        ev("2018-03-01", "pr_review", "renovate[bot]", author_association="NONE"),
        ev(
            "2018-04-01",
            "commit",
            None,
            source="git",
            actor_name="pyup.io bot",
            actor_email="github-bot@pyup.io",
        ),
    ]
    panel = pipeline(rows, bots, "2015-01-10", "2019-12-31")
    states, transitions = states_from_panel(panel, {}, window=12)
    assert transitions["to_state"].to_list() == [State.QUIET.value]
    assert State.REVIVED.value not in states["state_asof"].to_list()


def test_human_revival_counts(bots: BotFilter) -> None:
    rows = [
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2018-02-01", "commit", "newmaintainer", source="git"),
    ]
    # Ends before the revived spell could itself go quiet (Feb 2018 + 12 months).
    panel = pipeline(rows, bots, "2015-01-10", "2018-12-31")
    _, transitions = states_from_panel(panel, {}, window=12)
    assert transitions["to_state"].to_list() == [State.QUIET.value, State.REVIVED.value]


def test_reply_needs_maintainer_association(bots: BotFilter) -> None:
    rows = [
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2019-05-01", "issue_reply", "randomuser", author_association="NONE"),
        ev("2019-05-02", "issue_reply", "helper", author_association="CONTRIBUTOR"),
        ev("2019-05-03", "issue_reply", "coremaint", author_association="MEMBER"),
    ]
    panel = pipeline(rows, bots, "2015-01-10", "2019-12-31")
    may = panel.filter(pl.col("month") == month_index(dt.date(2019, 5, 1)))
    assert may["n_replies"].item() == 1
    assert may["n_active_maintainers"].item() == 1


def test_reply_fallback_without_association_is_as_of(bots: BotFilter) -> None:
    rows = [
        ev("2013-01-10", "release", None, source="pypi"),
        ev("2013-03-01", "issue_reply", "alice"),  # no association, no write evidence yet
        ev("2013-04-01", "push", "alice"),  # alice gets write evidence
        ev("2013-05-01", "issue_reply", "alice"),  # now counts
        ev("2013-05-02", "issue_reply", "jdoe"),  # repo owner always counts
    ]
    panel = pipeline(rows, bots, "2013-01-10", "2013-06-30")
    by_month = dict(zip(panel["month"].to_list(), panel["n_replies"].to_list(), strict=True))
    assert by_month[month_index(dt.date(2013, 3, 1))] == 0
    assert by_month[month_index(dt.date(2013, 4, 1))] == 0  # a push is not activity
    assert by_month[month_index(dt.date(2013, 5, 1))] == 2


def test_issue_opened_is_demand_not_activity(bots: BotFilter) -> None:
    rows = [
        ev("2015-01-10", "release", None, source="pypi"),
        ev("2017-01-05", "issue_opened", "user1", author_association="NONE"),
        ev("2017-01-06", "pr_opened", "user2", author_association="NONE"),
        ev("2017-01-07", "gh_release", "jdoe", author_association="OWNER"),
    ]
    panel = pipeline(rows, bots, "2015-01-10", "2017-12-31")
    jan = panel.filter(pl.col("month") == month_index(dt.date(2017, 1, 1)))
    assert jan["n_maintainer_activity"].item() == 0
    assert jan["n_issues_opened"].item() == 1
    assert jan["n_prs_opened"].item() == 1
