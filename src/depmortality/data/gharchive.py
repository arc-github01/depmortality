"""GH Archive: one-pass slim extraction to the owner's dataset, then cheap pilot reads.

Cost model (DECISIONS.md 2026-10-26): BigQuery bills the full size of every column read in
every table scanned; the repo filter does not reduce it. Reading ``payload`` dominates. So
we scan each year once, for the whole candidate repo set, and write a slim table.
"""

from __future__ import annotations

import datetime as dt
import re

import polars as pl

from depmortality.utils.bq import Param

EVENT_TYPES = (
    "IssuesEvent",
    "IssueCommentEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "ReleaseEvent",
    "PushEvent",
)
FIRST_YEAR = 2011  # GH Archive starts 2011-02


def extraction_sql(year: int, last_day: dt.date) -> str:
    """Slim projection of one year's monthly tables. Year bounds are literals so that
    BigQuery can prune tables (parameters in _TABLE_SUFFIX filters may not prune)."""
    if not FIRST_YEAR <= year <= last_day.year:
        raise ValueError(f"year {year} out of range")
    start = f"{year}01"
    end = f"{year}12" if year < last_day.year else f"{year}{last_day.month:02d}"
    types = ", ".join(f"'{t}'" for t in EVENT_TYPES)
    return f"""
    SELECT
      id AS event_id,
      type,
      created_at,
      repo.id AS repo_id,
      LOWER(repo.name) AS repo_name,
      actor.id AS actor_id,
      actor.login AS actor_login,
      JSON_VALUE(payload, '$.action') AS action,
      COALESCE(
        JSON_VALUE(payload, '$.comment.author_association'),
        JSON_VALUE(payload, '$.review.author_association'),
        IF(type = 'IssuesEvent', JSON_VALUE(payload, '$.issue.author_association'), NULL),
        IF(type = 'PullRequestEvent',
           JSON_VALUE(payload, '$.pull_request.author_association'), NULL)
      ) AS author_association,
      JSON_QUERY(payload, '$.issue.pull_request') IS NOT NULL AS on_pull_request,
      JSON_VALUE(payload, '$.ref') AS ref
    FROM `githubarchive.month.*`
    WHERE _TABLE_SUFFIX BETWEEN '{start}' AND '{end}'
      AND type IN ({types})
      AND (repo.id IN UNNEST(@repo_ids) OR LOWER(repo.name) IN UNNEST(@repo_names))
    """


def extraction_params(repo_ids: list[int], repo_names: list[str]) -> list[Param]:
    return [
        Param("repo_ids", "ARRAY<INT64>", sorted(set(repo_ids))),
        Param("repo_names", "ARRAY<STRING>", sorted({n.lower() for n in repo_names})),
    ]


_IDENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _checked(project: str, dataset: str) -> tuple[str, str]:
    if not (_IDENT.match(project) and _IDENT.match(dataset)):
        raise ValueError(f"unsafe BigQuery project/dataset: {project!r}.{dataset!r}")
    return project, dataset


def destination(project: str, dataset: str, year: int) -> str:
    project, dataset = _checked(project, dataset)
    return f"{project}.{dataset}.gha_slim_{year}"


def pilot_read_sql(project: str, dataset: str) -> str:
    project, dataset = _checked(project, dataset)
    return f"""
    SELECT * FROM `{project}.{dataset}.gha_slim_*`
    WHERE repo_id IN UNNEST(@repo_ids) OR repo_name IN UNNEST(@repo_names)
    """


def to_events(
    raw: pl.DataFrame, repo_to_packages: dict[str, list[str]], id_to_repo: dict[int, str]
) -> pl.DataFrame:
    """Map slim GH Archive rows onto panel events (one row per package sharing the repo)."""
    df = raw.with_columns(
        pl.col("repo_id")
        .replace_strict(id_to_repo, default=None, return_dtype=pl.String)
        .alias("_by_id"),
    ).with_columns(pl.coalesce("_by_id", "repo_name").alias("repo"))
    kind = (
        pl.when((pl.col("type") == "IssueCommentEvent") & pl.col("on_pull_request"))
        .then(pl.lit("pr_reply"))
        .when(pl.col("type") == "IssueCommentEvent")
        .then(pl.lit("issue_reply"))
        .when(pl.col("type") == "PullRequestReviewCommentEvent")
        .then(pl.lit("pr_reply"))
        .when(pl.col("type") == "PullRequestReviewEvent")
        .then(pl.lit("pr_review"))
        .when((pl.col("type") == "IssuesEvent") & (pl.col("action") == "opened"))
        .then(pl.lit("issue_opened"))
        .when((pl.col("type") == "PullRequestEvent") & (pl.col("action") == "opened"))
        .then(pl.lit("pr_opened"))
        .when(pl.col("type") == "PushEvent")
        .then(pl.lit("push"))
        .when(
            (pl.col("type") == "ReleaseEvent")
            & (pl.col("action").is_in(["published", "created"]) | pl.col("action").is_null())
        )
        .then(pl.lit("gh_release"))
        .otherwise(None)
    )
    # Comment edits/deletions are not new replies.
    keep_action = pl.col("action").is_null() | pl.col("action").is_in(
        ["created", "submitted", "opened", "published"]
    )
    mapping = pl.DataFrame(
        [(repo, pkg) for repo, pkgs in repo_to_packages.items() for pkg in pkgs],
        schema={"repo": pl.String, "package": pl.String},
        orient="row",
    )
    return (
        df.with_columns(kind.alias("kind"))
        .filter(pl.col("kind").is_not_null() & keep_action)
        .join(mapping, on="repo", how="inner")
        .unique(subset=["package", "event_id"], keep="first", maintain_order=True)
        .select(
            "package",
            "repo",
            pl.col("created_at").cast(pl.Date).alias("day"),
            pl.lit("gha").alias("source"),
            "kind",
            "actor_login",
            pl.lit(None, dtype=pl.String).alias("actor_name"),
            pl.lit(None, dtype=pl.String).alias("actor_email"),
            "author_association",
        )
    )
