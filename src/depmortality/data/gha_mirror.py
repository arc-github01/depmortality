"""GH Archive events from the public ClickHouse mirror (play.clickhouse.com, table
``github_events``), a free alternative to the multi-TB BigQuery scan (DECISIONS.md).

Verified against BigQuery on 2026-10-26: per-type event counts match on sampled days.
Differences from BigQuery that matter here:
- no repo id, only the repo name at event time (renamed repos: earlier names are missed);
- ``author_association`` is an enum whose ``NONE`` also encodes "not recorded", so it is
  set to null outside the months when GH Archive recorded it (``ASSOCIATION_WINDOW``);
- no flag separating comments on issues from comments on PRs, so IssueCommentEvent maps to
  ``issue_reply`` (both kinds count as maintainer replies).

Output rows use the slim schema of ``gharchive.extraction_sql`` so ``gharchive.to_events``
applies unchanged.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from collections.abc import Sequence

import polars as pl

from depmortality.data.gharchive import EVENT_TYPES
from depmortality.utils.cache import DiskCache
from depmortality.utils.http import HttpClient

log = logging.getLogger(__name__)

URL = "https://play.clickhouse.com/?user=play"
BATCH = 50
# First and last months in which GH Archive payloads carry author_association
# (measured on the mirror 2026-10-26: 0% before 2017-08, ~50% from 2017-09, 0% after 2025-10).
ASSOCIATION_WINDOW = (dt.date(2017, 9, 1), dt.date(2025, 9, 30))
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LIMIT_HINTS = ("TOO_MANY_ROWS", "TOO_SLOW", "TIMEOUT_EXCEEDED", "Limit for result exceeded")

SLIM_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "event_id": pl.String,
    "type": pl.String,
    "created_at": pl.Datetime("us"),
    "repo_id": pl.Int64,
    "repo_name": pl.String,
    "actor_id": pl.Int64,
    "actor_login": pl.String,
    "action": pl.String,
    "author_association": pl.String,
    "on_pull_request": pl.Boolean,
    "ref": pl.String,
}


class MirrorLimitError(RuntimeError):
    pass


def _quote(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe repo name {name!r}")
    return "'" + name + "'"


def query_sql(names: Sequence[str], start: dt.date, end: dt.date) -> str:
    types = ", ".join(f"'{t}'" for t in EVENT_TYPES)
    repos = ", ".join(_quote(n) for n in sorted(set(names)))
    return (
        "SELECT toString(event_type) AS type, actor_login, repo_name, created_at, "
        "toString(action) AS action, number, comment_id, "
        "toString(author_association) AS author_association, ref "
        f"FROM github_events WHERE event_type IN ({types}) AND repo_name IN ({repos}) "
        f"AND created_at >= '{start.isoformat()} 00:00:00' "
        f"AND created_at < '{(end + dt.timedelta(days=1)).isoformat()} 00:00:00' "
        "FORMAT Parquet"
    )


def to_slim(raw: pl.DataFrame) -> pl.DataFrame:
    if raw.height == 0:
        return pl.DataFrame(schema=SLIM_SCHEMA)
    lo, hi = ASSOCIATION_WINDOW
    day = pl.col("created_at").cast(pl.Date)
    assoc = (
        pl.when(day.is_between(lo, hi))
        .then(pl.col("author_association"))
        .otherwise(pl.lit(None, dtype=pl.String))
    )
    event_id = pl.concat_str(
        [
            pl.col("type"),
            pl.col("repo_name"),
            pl.col("created_at").cast(pl.String),
            pl.col("actor_login"),
            pl.col("number").cast(pl.String),
            pl.col("comment_id").cast(pl.String),
            pl.col("action"),
        ],
        separator="|",
    )
    return raw.select(
        event_id.alias("event_id"),
        pl.col("type"),
        pl.col("created_at").cast(pl.Datetime("us")),
        pl.lit(None, dtype=pl.Int64).alias("repo_id"),
        pl.col("repo_name").str.to_lowercase(),
        pl.lit(None, dtype=pl.Int64).alias("actor_id"),
        pl.col("actor_login").cast(pl.String),
        pl.when(pl.col("action") == "none").then(None).otherwise(pl.col("action")).alias("action"),
        assoc.alias("author_association"),
        pl.lit(False).alias("on_pull_request"),
        pl.when(pl.col("ref") == "")
        .then(None)
        .otherwise(pl.col("ref"))
        .cast(pl.String)
        .alias("ref"),
    ).unique(subset="event_id", keep="first", maintain_order=True)


class MirrorClient:
    def __init__(self, http: HttpClient, cache: DiskCache) -> None:
        self.http = http
        self.cache = cache

    def _query(self, names: Sequence[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        sql = query_sql(names, start, end)

        def fetch() -> bytes:
            # HTTP 500 is a query error (e.g. a result-size limit): don't retry it.
            r = self.http.request(
                "POST", URL, content=sql.encode(), retry_statuses=frozenset({429, 502, 503, 504})
            )
            if r.status_code != 200:
                text = r.text[:300]
                if any(h in text for h in _LIMIT_HINTS):
                    raise MirrorLimitError(text)
                raise RuntimeError(f"mirror HTTP {r.status_code}: {text}")
            return r.content

        data = self.cache.get_bytes("gha_mirror", {"sql": sql}, fetch, suffix="parquet")
        return pl.read_parquet(io.BytesIO(data)) if data else pl.DataFrame()

    def _pull(self, names: list[str], start: dt.date, end: dt.date) -> list[pl.DataFrame]:
        try:
            return [self._query(names, start, end)]
        except MirrorLimitError:
            if len(names) > 1:
                mid = len(names) // 2
                return self._pull(names[:mid], start, end) + self._pull(names[mid:], start, end)
            if (end - start).days < 32:
                raise
            mid_day = start + (end - start) / 2
            log.info("splitting %s at %s", names[0], mid_day)
            return self._pull(names, start, mid_day) + self._pull(
                names, mid_day + dt.timedelta(days=1), end
            )

    def events(self, names: Sequence[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        ordered = sorted(set(names))
        frames: list[pl.DataFrame] = []
        for i in range(0, len(ordered), BATCH):
            frames += self._pull(ordered[i : i + BATCH], start, end)
            if (i // BATCH) % 10 == 0:
                log.info("mirror: %d/%d repo names", min(i + BATCH, len(ordered)), len(ordered))
        raw = (
            pl.concat([f for f in frames if f.height], how="vertical_relaxed")
            if frames
            else pl.DataFrame()
        )
        return to_slim(raw)
