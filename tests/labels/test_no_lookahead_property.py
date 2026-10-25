"""Appending activity dated after month M never changes as_of states or panel values <= M."""

from __future__ import annotations

import datetime as dt

import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from depmortality.labels.bots import BotFilter
from depmortality.labels.panel import EVENT_SCHEMA, build_panel, flag_bots, flag_maintainer_activity
from depmortality.labels.states import Failure, compute_states
from depmortality.utils.months import month_index, month_start

O = 2015 * 12  # Jan 2015
H = 72  # follow-up months


@st.composite
def timelines(draw: st.DrawFn) -> tuple[list[int], Failure | None, int, int]:
    window = draw(st.integers(1, 18))
    acts = draw(st.lists(st.integers(O, O + H), max_size=25))
    fail = draw(st.one_of(st.none(), st.integers(O - 3, O + H + 5)))
    cut = draw(st.integers(O, O + H))
    failure = None if fail is None else Failure(fail, "archived")
    return acts, failure, window, cut


@given(timelines())
@settings(max_examples=400, deadline=None)
def test_state_asof_ignores_future(case: tuple[list[int], Failure | None, int, int]) -> None:
    acts, failure, window, cut = case
    full = compute_states(O, O + H, acts, failure, window)
    seen_failure = failure if failure is not None and failure.month <= cut else None
    past = compute_states(O, cut, [a for a in acts if a <= cut], seen_failure, window)
    assert past.state_asof == full.state_asof[: cut - O + 1]
    # Transitions knowable by the cut are identical too (needed for training labels).
    knowable = [t for t in full.transitions if t.observable_month <= cut]
    assert past.transitions == knowable


KINDS = ["commit", "release", "issue_reply", "pr_reply", "pr_review", "issue_opened", "push"]
LOGINS = ["jdoe", "alice", "bob", "dependabot[bot]", "pyup-bot", None]
ASSOC = [None, "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE"]


@st.composite
def event_rows(draw: st.DrawFn, lo: int, hi: int) -> list[dict[str, object]]:
    n = draw(st.integers(0, 30))
    rows = []
    for _ in range(n):
        month = draw(st.integers(lo, hi))
        day = month_start(month) + dt.timedelta(days=draw(st.integers(0, 27)))
        login = draw(st.sampled_from(LOGINS))
        rows.append(
            {
                "package": "pkg",
                "repo": "jdoe/pkg",
                "day": day,
                "source": "gha",
                "kind": draw(st.sampled_from(KINDS)),
                "actor_login": login,
                "actor_name": None,
                "actor_email": None,
                "author_association": draw(st.sampled_from(ASSOC)),
                "person": None if login is None else f"p:{login}",
            }
        )
    return rows


SPANS = pl.DataFrame(
    {"package": ["pkg"], "origin_month": [O], "end_month": [O + H]},
    schema={"package": pl.String, "origin_month": pl.Int32, "end_month": pl.Int32},
)


def _panel(rows: list[dict[str, object]], bots: BotFilter) -> pl.DataFrame:
    events = pl.DataFrame(rows, schema=EVENT_SCHEMA)
    return build_panel(flag_maintainer_activity(flag_bots(events, bots)), SPANS)


@given(st.data(), st.integers(O, O + H - 1))
@settings(max_examples=150, deadline=None)
def test_panel_ignores_future_rows(data: st.DataObject, cut: int) -> None:
    from depmortality.config import load_settings

    bots = BotFilter.from_config(load_settings().bots)
    past_rows = data.draw(event_rows(O - 6, cut))
    future_rows = data.draw(event_rows(cut + 1, O + H + 6))
    before = _panel(past_rows, bots).filter(pl.col("month") <= cut)
    after = _panel(past_rows + future_rows, bots).filter(pl.col("month") <= cut)
    assert before.equals(after)
    assert month_index(month_start(cut)) == cut
