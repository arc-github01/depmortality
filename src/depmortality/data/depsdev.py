"""deps.dev (BigQuery) queries and pilot selection.

The column names below are our reading of the deps_dev_v1 schema. ``REQUIRED_COLUMNS`` is
checked against INFORMATION_SCHEMA before any real query runs, so a mismatch fails loudly
instead of silently returning nothing.
"""

from __future__ import annotations

import datetime as dt
import math
import re

import numpy as np
import polars as pl

from depmortality.config import PilotConfig
from depmortality.utils.bq import Param
from depmortality.utils.seeding import rng

DATASET = "bigquery-public-data.deps_dev_v1"
PYPI_DATASET = "bigquery-public-data.pypi"

REQUIRED_COLUMNS: dict[str, set[str]] = {
    f"{DATASET}.Snapshots": {"Time"},
    f"{DATASET}.PackageVersionToProject": {
        "SnapshotAt",
        "System",
        "Name",
        "Version",
        "ProjectType",
        "ProjectName",
        "RelationType",
    },
    f"{DATASET}.Dependencies": {
        "SnapshotAt",
        "System",
        "Name",
        "Version",
        "Dependency",
        "MinimumDepth",
    },
    f"{PYPI_DATASET}.distribution_metadata": {
        "name",
        "version",
        "upload_time",
        "requires_python",
        "classifiers",
        "filename",
    },
}

_GITHUB_PROJECT = re.compile(r"^github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


def normalise_name(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def github_repo(project_name: str) -> str | None:
    """'github.com/Owner/Repo' -> 'owner/repo' (lower-cased), else None."""
    m = _GITHUB_PROJECT.match(project_name.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    return f"{owner}/{repo}".lower()


# ---- SQL ----------------------------------------------------------------------------------
def sql_schema_check() -> tuple[str, list[Param]]:
    sql = f"""
    SELECT CONCAT(table_catalog, '.', table_schema, '.', table_name) AS tbl, column_name
    FROM `bigquery-public-data.deps_dev_v1.INFORMATION_SCHEMA.COLUMNS`
    UNION ALL
    SELECT CONCAT(table_catalog, '.', table_schema, '.', table_name) AS tbl, column_name
    FROM `{PYPI_DATASET}.INFORMATION_SCHEMA.COLUMNS`
    WHERE table_name = 'distribution_metadata'
    """
    return sql, []


def missing_columns(found: pl.DataFrame) -> dict[str, set[str]]:
    have: dict[str, set[str]] = {}
    for tbl, col in found.select("tbl", "column_name").iter_rows():
        have.setdefault(str(tbl), set()).add(str(col))
    return {
        t: cols - have.get(t, set())
        for t, cols in REQUIRED_COLUMNS.items()
        if cols - have.get(t, set())
    }


def sql_latest_snapshot() -> tuple[str, list[Param]]:
    return f"SELECT MAX(Time) AS snapshot FROM `{DATASET}.Snapshots`", []


def sql_repo_mapping(snapshot: dt.datetime) -> tuple[str, list[Param]]:
    sql = f"""
    SELECT Name AS name, ProjectName AS project, RelationType AS relation,
           COUNT(DISTINCT Version) AS n_versions
    FROM `{DATASET}.PackageVersionToProject`
    WHERE SnapshotAt = @snapshot AND System = 'PYPI' AND ProjectType = 'GITHUB'
    GROUP BY name, project, relation
    """
    return sql, [Param("snapshot", "TIMESTAMP", snapshot)]


def sql_direct_dependents(snapshot: dt.datetime) -> tuple[str, list[Param]]:
    """Distinct dependent package names with a direct edge to each PyPI package."""
    sql = f"""
    SELECT Dependency.Name AS name, COUNT(DISTINCT Name) AS direct_dependents
    FROM `{DATASET}.Dependencies`
    WHERE SnapshotAt = @snapshot AND System = 'PYPI' AND Dependency.System = 'PYPI'
      AND MinimumDepth = 1
    GROUP BY name
    """
    return sql, [Param("snapshot", "TIMESTAMP", snapshot)]


def sql_first_upload() -> tuple[str, list[Param]]:
    sql = f"""
    SELECT REGEXP_REPLACE(LOWER(name), r'[-_.]+', '-') AS name, MIN(upload_time) AS first_upload
    FROM `{PYPI_DATASET}.distribution_metadata`
    GROUP BY name
    """
    return sql, []


def sql_release_classifiers(names: list[str]) -> tuple[str, list[Param]]:
    sql = f"""
    SELECT REGEXP_REPLACE(LOWER(name), r'[-_.]+', '-') AS name, version,
           MIN(upload_time) AS first_upload, ANY_VALUE(classifiers) AS classifiers
    FROM `{PYPI_DATASET}.distribution_metadata`
    WHERE REGEXP_REPLACE(LOWER(name), r'[-_.]+', '-') IN UNNEST(@names)
    GROUP BY name, version
    """
    return sql, [Param("names", "ARRAY<STRING>", sorted(names))]


# ---- universe and pilot --------------------------------------------------------------------
def choose_repo(mapping: pl.DataFrame) -> pl.DataFrame:
    """One GitHub repo per package: prefer a source-repo relation, then most versions."""
    rows = (
        mapping.with_columns(
            pl.col("name").map_elements(normalise_name, return_dtype=pl.String),
            pl.col("project")
            .map_elements(lambda p: github_repo(p) or "", return_dtype=pl.String)
            .alias("repo"),
            pl.col("relation").str.contains("SOURCE").fill_null(False).alias("_source"),
        )
        .filter(pl.col("repo") != "")
        .sort(["name", "_source", "n_versions", "repo"], descending=[False, True, True, False])
    )
    return rows.group_by("name", maintain_order=True).first().select("name", "repo")


def build_universe(
    mapping: pl.DataFrame,
    dependents: pl.DataFrame,
    first_upload: pl.DataFrame,
    cfg: PilotConfig,
) -> pl.DataFrame:
    """Qualifying packages: >= 1 direct dependent, a GitHub repo, first upload in the cohort."""
    deps = (
        dependents.with_columns(pl.col("name").map_elements(normalise_name, return_dtype=pl.String))
        .group_by("name")
        .agg(pl.col("direct_dependents").sum())
    )
    first = first_upload.with_columns(pl.col("first_upload").cast(pl.Date))
    universe = (
        choose_repo(mapping)
        .join(deps, on="name", how="inner")
        .join(first, on="name", how="inner")
        .filter(pl.col("direct_dependents") >= 1)
        .filter(
            pl.col("first_upload").is_between(
                cfg.cohort_first_release_start, cfg.cohort_first_release_end
            )
        )
    )
    repo_sizes = universe.group_by("repo").len("packages_in_repo")
    return universe.join(repo_sizes, on="repo").sort("name")


def _bin(count: int, edges: list[int]) -> str:
    for lo, hi in zip(edges, [*edges[1:], math.inf], strict=True):
        if lo <= count < hi:
            return f"{lo}+" if hi == math.inf else f"{lo}-{int(hi) - 1}"
    return f"<{edges[0]}"


def select_pilot(universe: pl.DataFrame, cfg: PilotConfig) -> pl.DataFrame:
    """Top-n census plus a proportionally allocated stratified tail sample, with weights."""
    ranked = universe.sort(["direct_dependents", "name"], descending=[True, False])
    top = ranked.head(cfg.n_top).with_columns(
        pl.lit("top").alias("stratum"), pl.lit(1.0).alias("weight")
    )
    tail = ranked.slice(cfg.n_top).with_columns(
        (
            pl.col("direct_dependents").map_elements(
                lambda c: _bin(int(c), cfg.tail_dependent_bin_edges), return_dtype=pl.String
            )
            + "|"
            + pl.col("first_upload").dt.year().cast(pl.String)
        ).alias("stratum")
    )
    if tail.height == 0 or cfg.n_tail == 0:
        return top.with_columns(pl.lit("top").alias("selected_as"))
    sizes = tail.group_by("stratum").len().sort("stratum")
    total = int(sizes["len"].sum())
    n_tail = min(cfg.n_tail, total)
    alloc = _allocate(dict(zip(sizes["stratum"], sizes["len"], strict=True)), n_tail)
    picks = []
    for stratum, n in sorted(alloc.items()):
        members = tail.filter(pl.col("stratum") == stratum).sort("name")
        gen = rng(cfg.seed, "pilot-tail", stratum)
        idx = np.sort(gen.choice(members.height, size=n, replace=False))
        picks.append(members[idx.tolist()].with_columns(pl.lit(members.height / n).alias("weight")))
    sample = pl.concat(picks)
    selected: pl.DataFrame = (
        pl.concat([top, sample], how="vertical_relaxed")
        .with_columns(
            pl.when(pl.col("stratum") == "top")
            .then(pl.lit("top"))
            .otherwise(pl.lit("tail"))
            .alias("selected_as")
        )
        .sort("name")
    )
    return selected


def _allocate(sizes: dict[str, int], n: int) -> dict[str, int]:
    """Proportional allocation with >= 1 per non-empty stratum (largest-remainder rounding)."""
    strata = sorted(s for s, k in sizes.items() if k > 0)
    if n < len(strata):
        raise ValueError(f"tail sample {n} is smaller than the number of strata {len(strata)}")
    total = sum(sizes[s] for s in strata)
    alloc = {s: 1 for s in strata}
    remaining = n - len(strata)
    quotas = {s: remaining * sizes[s] / total for s in strata}
    for s in strata:
        alloc[s] += min(int(quotas[s]), sizes[s] - 1)
    left = n - sum(alloc.values())
    order = sorted(strata, key=lambda s: (-(quotas[s] - int(quotas[s])), s))
    while left > 0:
        progressed = False
        for s in order:
            if left and alloc[s] < sizes[s]:
                alloc[s] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return alloc
