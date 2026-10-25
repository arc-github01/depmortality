"""Monthly (package, month) activity panel, computed AFTER bot removal.

Input: a long event table with columns
    package, repo, day (Date), source, kind, actor_login, actor_name, actor_email,
    author_association, person
where ``kind`` is one of EVENT_KINDS and ``person`` is a (hashed) person ID or null.

Maintainer activity (CLAUDE.md section 3) is a non-bot commit to the default branch, a PyPI
release, or a maintainer reply/review on an issue or PR. A replier is a maintainer if GH
Archive's ``author_association`` is OWNER/MEMBER/COLLABORATOR. Where it is missing (older
events), the fallback is as_of-aware: the replier is the repo owner, or had pushed to the
repo or carried a maintainer association on or before that day.
"""

from __future__ import annotations

import polars as pl

from depmortality.labels.bots import BotFilter

EVENT_KINDS = (
    "commit",  # git: commit on the default branch (committer date)
    "release",  # PyPI: first upload of a version
    "issue_reply",  # GH Archive: IssueCommentEvent on an issue
    "pr_reply",  # GH Archive: IssueCommentEvent on a PR, PullRequestReviewCommentEvent
    "pr_review",  # GH Archive: PullRequestReviewEvent
    "issue_opened",
    "pr_opened",
    "push",  # GH Archive: PushEvent (write-access evidence only, not activity)
    "gh_release",  # GH Archive: ReleaseEvent (recorded, not activity)
)
REPLY_KINDS = ("issue_reply", "pr_reply", "pr_review")
MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")

EVENT_SCHEMA = {
    "package": pl.String,
    "repo": pl.String,
    "day": pl.Date,
    "source": pl.String,
    "kind": pl.String,
    "actor_login": pl.String,
    "actor_name": pl.String,
    "actor_email": pl.String,
    "author_association": pl.String,
    "person": pl.String,
}

PANEL_COUNTS = (
    "n_commits",
    "n_releases",
    "n_replies",
    "n_reviews",
    "n_issues_opened",
    "n_prs_opened",
    "n_bot_events",
    "n_maintainer_activity",
    "n_active_maintainers",
)


def month_expr(col: str) -> pl.Expr:
    return pl.col(col).dt.year().cast(pl.Int32) * 12 + pl.col(col).dt.month().cast(pl.Int32) - 1


def flag_bots(events: pl.DataFrame, bots: BotFilter) -> pl.DataFrame:
    """Adds ``is_bot``. Evaluated once per distinct (login, name, email)."""
    keys = events.select("actor_login", "actor_name", "actor_email").unique()
    flags = [bots.is_bot(login=r[0], name=r[1], email=r[2]) for r in keys.iter_rows()]
    keyed = keys.with_columns(pl.Series("is_bot", flags, dtype=pl.Boolean))
    return events.join(
        keyed, on=["actor_login", "actor_name", "actor_email"], how="left", nulls_equal=True
    )


def flag_maintainer_activity(events: pl.DataFrame) -> pl.DataFrame:
    """Adds ``is_maintainer_activity``. Requires ``is_bot``."""
    human = events.filter(~pl.col("is_bot"))
    login = pl.col("actor_login").str.to_lowercase()
    write_evidence = (
        human.filter(
            (pl.col("kind") == "push") | pl.col("author_association").is_in(MAINTAINER_ASSOCIATIONS)
        )
        .filter(pl.col("actor_login").is_not_null())
        .group_by("package", login.alias("_login"))
        .agg(pl.col("day").min().alias("_first_write"))
    )
    owner = pl.col("repo").str.split("/").list.first().str.to_lowercase()
    out = events.with_columns(login.alias("_login")).join(
        write_evidence, on=["package", "_login"], how="left"
    )
    fallback = (pl.col("_login") == owner) | (
        pl.col("_first_write").is_not_null() & (pl.col("_first_write") <= pl.col("day"))
    )
    reply_ok = (
        pl.when(pl.col("author_association").is_null())
        .then(fallback.fill_null(False))
        .otherwise(pl.col("author_association").is_in(MAINTAINER_ASSOCIATIONS))
    )
    is_activity = (
        pl.when(pl.col("is_bot"))
        .then(False)
        .when(pl.col("kind") == "commit")
        .then(True)
        .when(pl.col("kind") == "release")
        .then(True)
        .when(pl.col("kind").is_in(REPLY_KINDS))
        .then(reply_ok)
        .otherwise(False)
    )
    return out.with_columns(is_activity.alias("is_maintainer_activity")).drop(
        "_login", "_first_write"
    )


def build_panel(events: pl.DataFrame, spans: pl.DataFrame) -> pl.DataFrame:
    """One row per (package, month) from ``origin_month`` to ``end_month`` (inclusive).

    ``events`` needs ``is_bot`` and ``is_maintainer_activity``; ``spans`` has
    package, origin_month, end_month (month indices). Events outside the span are dropped.
    """
    grid = (
        spans.select(
            "package",
            pl.int_ranges("origin_month", pl.col("end_month") + 1, dtype=pl.Int32).alias("month"),
        )
        .explode("month")
        .drop_nulls("month")
    )
    ev = events.with_columns(month_expr("day").alias("month")).join(
        spans, on="package", how="inner"
    )
    ev = ev.filter(pl.col("month").is_between(pl.col("origin_month"), pl.col("end_month")))
    act = pl.col("is_maintainer_activity")
    human = ~pl.col("is_bot")
    kind = pl.col("kind")
    counts = ev.group_by("package", "month").agg(
        (act & (kind == "commit")).sum().alias("n_commits"),
        (kind == "release").sum().alias("n_releases"),
        (act & kind.is_in(("issue_reply", "pr_reply"))).sum().alias("n_replies"),
        (act & (kind == "pr_review")).sum().alias("n_reviews"),
        (human & (kind == "issue_opened")).sum().alias("n_issues_opened"),
        (human & (kind == "pr_opened")).sum().alias("n_prs_opened"),
        pl.col("is_bot").sum().alias("n_bot_events"),
        act.sum().alias("n_maintainer_activity"),
        pl.col("person")
        .filter(act & pl.col("person").is_not_null())
        .n_unique()
        .alias("n_active_maintainers"),
    )
    panel = grid.join(counts, on=["package", "month"], how="left")
    return panel.with_columns([pl.col(c).fill_null(0).cast(pl.Int32) for c in PANEL_COUNTS]).sort(
        "package", "month"
    )
