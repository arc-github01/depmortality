"""Phase 0 pipeline. ``python -m depmortality.pipeline <stage> [--refresh | --cache-only]``.

Stages (in order): dryrun, universe, resolve, select, extract, pull, build, report.
``pilot`` runs universe..report. With ``--cache-only`` every external response must already
be cached, so ``pilot --cache-only`` rebuilds everything from data/raw with no network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from depmortality.config import Settings, load_settings
from depmortality.data import depsdev, gharchive
from depmortality.data.gha_mirror import MirrorClient
from depmortality.data.github import GitHubClient
from depmortality.data.gitlog import COMMIT_SCHEMA, CloneFailed, commits_frame
from depmortality.data.osv import OSVClient
from depmortality.data.pypi import PyPIClient
from depmortality.labels.identity import MissingSalt
from depmortality.utils.bq import GB, DryRun, GuardedBigQuery, Param, QueryRefused, make_client
from depmortality.utils.cache import CacheMiss, DiskCache
from depmortality.utils.http import HttpClient, MissingCredential
from depmortality.utils.logsetup import setup_logging
from depmortality.utils.paths import REPO_ROOT, DataPaths, default_paths, reports_dir

log = logging.getLogger("depmortality.pipeline")
PHASE = "phase0"
GIT_WORKERS = 2  # 4 ran the machine low on memory (2026-10-26)


@dataclass
class Context:
    settings: Settings
    paths: DataPaths
    cache: DiskCache
    cache_only: bool
    refresh: bool
    _bq: GuardedBigQuery | None = None
    _http: HttpClient | None = None
    status_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def bq(self) -> GuardedBigQuery:
        if self._bq is None:
            client = None if self.cache_only else make_client()
            self._bq = GuardedBigQuery(
                client,
                self.settings.thresholds.bigquery,
                self.settings.bq_approvals,
                self.paths.bq_ledger,
                phase=PHASE,
                cache=self.cache,
            )
        return self._bq

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(self.settings.thresholds.http)
        return self._http

    def interim(self, name: str) -> Path:
        return self.paths.interim / f"{name}.parquet"

    def status(self, package: str, step: str, ok: bool, reason: str = "") -> None:
        self.status_rows.append({"package": package, "step": step, "ok": ok, "reason": reason})

    def close(self) -> None:
        if self._http is not None:
            calls = dict(self._http.calls)
            if calls:
                with self.paths.api_ledger.open("a", encoding="utf-8") as fh:
                    entry = {
                        "ts": dt.datetime.now(dt.UTC).isoformat(),
                        "phase": PHASE,
                        "calls": calls,
                    }
                    fh.write(json.dumps(entry) + "\n")
            self._http.close()


def _frame(table: Any) -> pl.DataFrame:
    frame = pl.from_arrow(table)
    if not isinstance(frame, pl.DataFrame):
        raise TypeError("expected a table, got a column")
    return frame


def _dataset() -> tuple[str, str]:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    dataset = os.environ.get("DEPMORTALITY_BQ_DATASET", "depmortality").strip()
    if not project:
        raise QueryRefused("GOOGLE_CLOUD_PROJECT is not set")
    return project, dataset


# ---- stage: dryrun ------------------------------------------------------------------------
def stage_dryrun(ctx: Context) -> None:
    """Dry-run (free) every Phase 0 query whose inputs exist; write the approval sheet."""
    bq = ctx.bq
    found = bq.run(*depsdev.sql_schema_check(), "schema_check")
    missing = depsdev.missing_columns(_frame(found))
    snapshot = _latest_snapshot(ctx)
    planned: list[tuple[str, str, list[Param]]] = [
        ("Q2 repo mapping", *depsdev.sql_repo_mapping(snapshot)),
        ("Q3 direct dependents", *depsdev.sql_direct_dependents(snapshot)),
        ("Q4a first upload", *depsdev.sql_first_upload()),
    ]
    pending: list[str] = []
    pilot_path = ctx.interim("pilot")
    if pilot_path.exists():
        names = pl.read_parquet(pilot_path)["name"].to_list()
        planned.append(("Q4b release classifiers (pilot)", *depsdev.sql_release_classifiers(names)))
    else:
        pending.append("Q4b release classifiers — needs the pilot list (stage `select`)")
    ids_names = _candidate_repo_params(ctx)
    freeze = ctx.settings.splits.data_freeze
    for year in range(2012, freeze.year + 1):
        label = f"Q5 GH Archive extraction {year}"
        if ids_names is None:
            pending.append(f"{label} — needs resolved candidate repos (stage `resolve`)")
            continue
        planned.append(
            (label, gharchive.extraction_sql(year, freeze), gharchive.extraction_params(*ids_names))
        )
    if ids_names is None:
        # Bytes do not depend on the repo list, so show the cost now with a placeholder list.
        for year in range(2012, freeze.year + 1):
            sql = gharchive.extraction_sql(year, freeze)
            planned.append(
                (
                    f"Q5 GH Archive {year} (cost preview, placeholder repos)",
                    sql,
                    gharchive.extraction_params([0], ["x/y"]),
                )
            )
    results: list[DryRun] = []
    errors: list[str] = []
    for label, sql, params in planned:
        try:
            results.append(bq.dry_run(sql, params, label))
        except Exception as exc:  # report, don't hide
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    _write_dryrun_report(ctx, results, pending, errors, missing, snapshot)


def _latest_snapshot(ctx: Context) -> dt.datetime:
    table = _frame(ctx.bq.run(*depsdev.sql_latest_snapshot(), "Q1 latest snapshot"))
    value = table["snapshot"].item()
    if not isinstance(value, dt.datetime):
        raise RuntimeError(f"unexpected snapshot value {value!r}")
    return value


def _write_dryrun_report(
    ctx: Context,
    results: list[DryRun],
    pending: list[str],
    errors: list[str],
    missing: dict[str, set[str]],
    snapshot: dt.datetime,
) -> None:
    cap = ctx.settings.thresholds.bigquery.max_gb_per_query
    lines = [
        "# BigQuery dry-run report — Phase 0",
        "",
        f"Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC by `pipeline dryrun`. "
        f"deps.dev snapshot: {snapshot:%Y-%m-%d %H:%M}. Dry-runs are free.",
        f"Per-query cap: {cap} GB. Phase budget: "
        f"{ctx.bq.phase_budget_bytes / GB:.0f} GB; "
        f"already billed this phase: {ctx.bq.spent_bytes() / GB:.3f} GB.",
        "",
    ]
    if missing:
        lines += ["## Schema mismatches (fix before running)", ""]
        lines += [f"- `{t}` lacks: {', '.join(sorted(c))}" for t, c in missing.items()]
        lines.append("")
    lines += [
        "## Estimates",
        "",
        "| Query | Estimated GB | Needs approval | sha256 |",
        "|---|---:|---|---|",
    ]
    total = 0
    for d in results:
        total += d.bytes_processed
        flag = "yes" if d.gb > cap else "no"
        lines.append(f"| {d.label} | {d.gb:,.3f} | {flag} | `{d.sql_sha256}` |")
    lines += ["", f"Total if all run: {total / GB:,.3f} GB.", ""]
    if pending:
        lines += ["## Not yet estimable", "", *[f"- {p}" for p in pending], ""]
    if errors:
        lines += ["## Dry-run errors", "", *[f"- {e}" for e in errors], ""]
    lines += [
        "## To approve",
        "",
        "Add an entry per query to `config/bq_approvals.yaml` (sha256 from the table, "
        "max_gb a little above the estimate), and raise `phase_budget_override_gb` "
        "to cover the total. Cost previews with placeholder repos are not approvable: "
        "their hash changes once the real repo list exists.",
    ]
    out = reports_dir() / "bq_dryrun_phase0.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote %s", out)


# ---- stage: universe ----------------------------------------------------------------------
def stage_universe(ctx: Context) -> None:
    bq = ctx.bq
    snapshot = _latest_snapshot(ctx)
    mapping = _frame(bq.run(*depsdev.sql_repo_mapping(snapshot), "Q2 repo mapping"))
    dependents = _frame(bq.run(*depsdev.sql_direct_dependents(snapshot), "Q3 direct dependents"))
    first = _frame(bq.run(*depsdev.sql_first_upload(), "Q4a first upload"))
    universe = depsdev.build_universe(mapping, dependents, first, ctx.settings.pilot)
    universe.write_parquet(ctx.interim("universe"))
    meta = {
        "depsdev_snapshot": snapshot.isoformat(),
        "packages": universe.height,
        "repos": universe["repo"].n_unique(),
    }
    (ctx.paths.interim / "universe_meta.json").write_text(json.dumps(meta, indent=2))
    log.info("universe: %s", meta)


# ---- stage: resolve -----------------------------------------------------------------------
def stage_resolve(ctx: Context) -> None:
    universe = pl.read_parquet(ctx.interim("universe"))
    gh = GitHubClient(ctx.http, ctx.cache)
    metas = gh.repos(universe["repo"].unique().to_list())
    frame = pl.DataFrame([asdict(m) for m in metas], infer_schema_length=None)
    frame.write_parquet(ctx.interim("universe_repos"))
    log.info("resolved %d repos; %d found", frame.height, frame["found"].sum())


def _candidate_repo_params(ctx: Context) -> tuple[list[int], list[str]] | None:
    path = ctx.interim("universe_repos")
    if not path.exists():
        return None
    repos = pl.read_parquet(path).filter(pl.col("found"))
    ids = [int(i) for i in repos["database_id"].drop_nulls().to_list()]
    names = repos["repo"].to_list() + [
        n.lower() for n in repos["name_with_owner"].drop_nulls().to_list()
    ]
    return ids, names


# ---- stage: select ------------------------------------------------------------------------
def stage_select(ctx: Context) -> None:
    universe = pl.read_parquet(ctx.interim("universe"))
    found = pl.read_parquet(ctx.interim("universe_repos")).filter(
        pl.col("found") & ~pl.col("is_fork")
    )
    eligible = universe.join(found.select("repo"), on="repo", how="semi")
    pilot = depsdev.select_pilot(eligible, ctx.settings.pilot)
    pilot.write_parquet(ctx.interim("pilot"))
    strata = eligible.height
    meta = {
        "universe_packages": universe.height,
        "eligible_packages": strata,
        "excluded_repo_not_found_or_fork": universe.height - strata,
        "pilot": pilot.height,
        "selection_rule": "DECISIONS.md 2026-10-26, Pilot selection",
        "seed": ctx.settings.pilot.seed,
        "snapshot_date": ctx.settings.pilot.snapshot_date.isoformat(),
    }
    (ctx.paths.interim / "pilot_meta.json").write_text(json.dumps(meta, indent=2))
    log.info("pilot: %s", meta)


# ---- stage: extract -----------------------------------------------------------------------
def stage_extract(ctx: Context) -> None:
    if ctx.settings.pilot.gha_source != "bigquery":
        log.info("gha_source=%s: no BigQuery extraction needed", ctx.settings.pilot.gha_source)
        return
    marker = ctx.paths.interim / "gha_extract_done.json"
    done: dict[str, str] = json.loads(marker.read_text()) if marker.exists() else {}
    if ctx.cache_only:
        log.info("cache-only: skipping extraction (done years: %s)", sorted(done))
        return
    params = _candidate_repo_params(ctx)
    if params is None:
        raise RuntimeError("run `resolve` first")
    project, dataset = _dataset()
    freeze = ctx.settings.splits.data_freeze
    for year in range(2012, freeze.year + 1):
        if str(year) in done and not ctx.refresh:
            continue
        sql = gharchive.extraction_sql(year, freeze)
        ctx.bq.run(
            sql,
            gharchive.extraction_params(*params),
            f"Q5 GH Archive extraction {year}",
            destination=gharchive.destination(project, dataset, year),
        )
        done[str(year)] = dt.datetime.now(dt.UTC).isoformat()
        marker.write_text(json.dumps(done, indent=2))


# ---- stage: pull --------------------------------------------------------------------------
def _try(ctx: Context, package: str, step: str, fn: Callable[[], Any]) -> Any:
    try:
        value = fn()
    except Exception as exc:
        ctx.status(package, step, False, f"{type(exc).__name__}: {exc}"[:300])
        return None
    ctx.status(package, step, True)
    return value


def stage_pull(ctx: Context) -> None:
    pilot = pl.read_parquet(ctx.interim("pilot"))
    names = pilot["name"].to_list()
    pkg_repo = dict(pilot.select("name", "repo").iter_rows())
    pypi = PyPIClient(ctx.http, ctx.cache)

    release_rows, meta_rows = [], []
    for name in names:
        info = _try(ctx, name, "pypi_json", partial(pypi.project, name))
        if info is None:
            continue
        status = _try(ctx, name, "pypi_status", partial(pypi.status, name))
        roles = _try(ctx, name, "pypi_roles", partial(pypi.roles, name)) or []
        for r in info.releases:
            release_rows.append(
                {
                    "package": name,
                    "version": r.version,
                    "upload_day": r.upload_day,
                    "yanked": r.yanked,
                    "requires_python": r.requires_python,
                    "wheels": [{"python": w.python, "abi": w.abi} for w in r.wheels],
                }
            )
        meta_rows.append(
            {
                "package": name,
                "status": status,
                "roles": [acct for _, acct in roles],
                "home_page": info.home_page,
                "n_releases": len(info.releases),
            }
        )
    pl.DataFrame(
        release_rows,
        schema={
            "package": pl.String,
            "version": pl.String,
            "upload_day": pl.Date,
            "yanked": pl.Boolean,
            "requires_python": pl.String,
            "wheels": pl.List(pl.Struct({"python": pl.String, "abi": pl.String})),
        },
    ).write_parquet(ctx.interim("releases"))
    pl.DataFrame(
        meta_rows,
        schema={
            "package": pl.String,
            "status": pl.String,
            "roles": pl.List(pl.String),
            "home_page": pl.String,
            "n_releases": pl.Int64,
        },
    ).write_parquet(ctx.interim("pypi_meta"))

    classifiers = _frame(
        ctx.bq.run(*depsdev.sql_release_classifiers(names), "Q4b release classifiers (pilot)")
    )
    classifiers.rename({"name": "package"}).select(
        "package", "version", "classifiers"
    ).write_parquet(ctx.interim("release_classifiers"))
    aliases = _repo_aliases(ctx, classifiers, pilot)
    aliases.write_parquet(ctx.interim("repo_aliases"))
    log.info(
        "repo aliases: %d earlier names for %d repos", aliases.height, aliases["repo"].n_unique()
    )

    # git: commit-only clones, in parallel; results and failures are cached.
    repos = sorted(set(pkg_repo.values()))
    ctx.paths.git_mirrors.mkdir(parents=True, exist_ok=True)

    def one(repo: str) -> tuple[str, pl.DataFrame | CloneFailed]:
        try:
            return repo, commits_frame(repo, ctx.cache, ctx.paths.git_mirrors)
        except CloneFailed as exc:
            return repo, exc

    # Columnar per repo, joined to packages once: millions of commits, several packages
    # per repo (DECISIONS.md: holding them as Python objects ran the machine out of memory).
    repo_frames: list[pl.DataFrame] = []
    failed: dict[str, CloneFailed] = {}
    with ThreadPoolExecutor(GIT_WORKERS) as pool:
        for repo, result in pool.map(one, repos):
            if isinstance(result, CloneFailed):
                failed[repo] = result
            else:
                repo_frames.append(result.with_columns(pl.lit(repo).alias("repo")))
    for name in names:
        exc = failed.get(pkg_repo[name])
        if exc is not None:
            ctx.status(name, "git", False, f"CloneFailed: {exc}"[:300])
        else:
            ctx.status(name, "git", True)
    repo_commits = (
        pl.concat(repo_frames)
        if repo_frames
        else pl.DataFrame(schema={**COMMIT_SCHEMA, "repo": pl.String})
    )
    del repo_frames
    commits = (
        pilot.select(pl.col("name").alias("package"), "repo")
        .join(repo_commits, on="repo", how="inner")
        .select("package", "repo", *COMMIT_SCHEMA)
    )
    del repo_commits
    commits.write_parquet(ctx.interim("commits"))

    # GitHub: repo metadata (from resolve) and email -> login for one commit per email.
    universe_repos = pl.read_parquet(ctx.interim("universe_repos"))
    universe_repos.join(pilot.select("repo").unique(), on="repo", how="semi").write_parquet(
        ctx.interim("github_repos")
    )
    gh = GitHubClient(ctx.http, ctx.cache)
    login_rows = []
    # Sorted so the GraphQL batches, and so their cache keys, are the same on every run.
    latest = (
        commits.sort("committer_day", "sha")
        .group_by("repo", "author_email")
        .last()
        .sort("repo", "sha")
    )
    for key, group in latest.group_by("repo", maintain_order=True):
        shas = group["sha"].to_list()
        pairs = (
            _try(
                ctx,
                str(key[0]),
                "github_commit_logins",
                partial(gh.commit_logins, str(key[0]), shas),
            )
            or []
        )
        login_rows += [{"repo": key[0], "email": e, "login": lg} for e, lg in pairs]
    pl.DataFrame(
        login_rows, schema={"repo": pl.String, "email": pl.String, "login": pl.String}
    ).write_parquet(ctx.interim("commit_logins"))
    del commits, latest  # not needed below; frees memory for the GH Archive pull

    # OSV advisories.
    osv = OSVClient(ctx.http, ctx.cache)
    advisories = _try(ctx, "*", "osv", partial(osv.dump_advisories, names))
    if advisories is None:
        # Without advisories trigger (b) cannot fire: missing data, not "no advisories".
        raise RuntimeError("OSV export could not be read; see pull_status")
    adv_rows = [
        {
            "package": n,
            "vuln_id": a.vuln_id,
            "published": a.published,
            "withdrawn": a.withdrawn,
            "fixed": list(a.fixed),
            "last_affected": list(a.last_affected),
            "affected_versions": list(a.affected_versions),
        }
        for n, advs in advisories.items()
        for a in advs
    ]
    pl.DataFrame(
        adv_rows,
        schema={
            "package": pl.String,
            "vuln_id": pl.String,
            "published": pl.Date,
            "withdrawn": pl.Date,
            "fixed": pl.List(pl.String),
            "last_affected": pl.List(pl.String),
            "affected_versions": pl.List(pl.String),
        },
    ).write_parquet(ctx.interim("advisories"))

    # GH Archive events for the pilot repos.
    found = universe_repos.join(pilot.select("repo").unique(), on="repo", how="semi").filter(
        pl.col("found")
    )
    repo_to_packages: dict[str, list[str]] = {}
    for name, repo in pkg_repo.items():
        repo_to_packages.setdefault(repo, []).append(name)
    # Renamed repos: GH Archive carries the name at event time; map current and earlier
    # names back to the deps.dev slug.
    for r, nwo in found.select("repo", "name_with_owner").drop_nulls().iter_rows():
        if nwo.lower() != r:
            repo_to_packages.setdefault(nwo.lower(), repo_to_packages.get(r, []))
    for r, alias in aliases.select("repo", "alias").iter_rows():
        if alias.lower() != r and r in repo_to_packages:
            repo_to_packages.setdefault(alias.lower(), repo_to_packages[r])
    if ctx.settings.pilot.gha_source == "clickhouse_mirror":
        _mirror_events(ctx, pilot, found, aliases, repo_to_packages)
    else:
        project, dataset = (
            _dataset()
            if not ctx.cache_only
            else (
                os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                os.environ.get("DEPMORTALITY_BQ_DATASET", "depmortality"),
            )
        )
        ids = [int(i) for i in found["database_id"].drop_nulls().to_list()]
        gh_names = found["repo"].to_list() + [
            n.lower() for n in found["name_with_owner"].drop_nulls().to_list()
        ]
        raw = _frame(
            ctx.bq.run(
                gharchive.pilot_read_sql(project, dataset),
                gharchive.extraction_params(ids, gh_names),
                "Q6 GH Archive pilot read",
            )
        )
        id_to_repo = {
            int(i): r for r, i in found.select("repo", "database_id").drop_nulls().iter_rows()
        }
        gharchive.to_events(raw, repo_to_packages, id_to_repo).write_parquet(
            ctx.interim("gha_events")
        )

    pl.DataFrame(
        ctx.status_rows,
        schema={"package": pl.String, "step": pl.String, "ok": pl.Boolean, "reason": pl.String},
    ).write_parquet(ctx.interim("pull_status"))
    counts = Counter((r["step"], r["ok"]) for r in ctx.status_rows)
    log.info("pull status: %s", dict(counts))


def _mirror_events(
    ctx: Context,
    pilot: pl.DataFrame,
    found: pl.DataFrame,
    aliases: pl.DataFrame,
    repo_to_packages: dict[str, list[str]],
) -> None:
    """GH Archive events from the ClickHouse mirror, written to disk one query at a time
    (the whole pull does not fit in memory on the pilot machine)."""
    # The mirror matches repo names case-sensitively: use the canonical GitHub name, the
    # deps.dev original-case slug and the recovered earlier names.
    case_names = pilot.join(found.select("repo"), on="repo", how="semi")["repo_case"]
    mirror_names = sorted(
        set(found["name_with_owner"].drop_nulls().to_list())
        | set(case_names.to_list())
        | set(aliases["alias"].to_list())
    )
    # A reused old name belongs to our repo only before the new repo was created.
    cutoffs = aliases.filter(pl.col("until").is_not_null()).select(
        pl.col("alias").str.to_lowercase().alias("repo_name"), "until"
    )
    parts = ctx.paths.interim / "gha_events_parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    mirror = MirrorClient(ctx.http, ctx.cache)
    start, end = ctx.settings.pilot.gha_start, ctx.settings.splits.data_freeze
    for i, slim in enumerate(mirror.iter_events(mirror_names, start, end)):
        kept = (
            slim.join(cutoffs, on="repo_name", how="left")
            .filter(
                pl.col("until").is_null() | (pl.col("created_at").cast(pl.Date) < pl.col("until"))
            )
            .drop("until")
        )
        gharchive.to_events(kept, repo_to_packages, {}).write_parquet(parts / f"{i:05d}.parquet")
    pl.scan_parquet(parts / "*.parquet").sink_parquet(ctx.interim("gha_events"))
    shutil.rmtree(parts, ignore_errors=True)


def _repo_aliases(ctx: Context, classifiers: pl.DataFrame, pilot: pl.DataFrame) -> pl.DataFrame:
    """Earlier GitHub names of pilot repos (free; DECISIONS.md, rename recovery).

    Candidates are GitHub URLs in the package's historical release metadata. GitHub resolves
    a renamed repo's old name to the same repository id, which confirms the alias. If the old
    name now belongs to a different, newer repository (a reused name), the alias holds only
    for events before that repository was created (``until``).
    """
    universe_repos = pl.read_parquet(ctx.interim("universe_repos")).filter(pl.col("found"))
    ids = dict(universe_repos.select("repo", "database_id").iter_rows())
    pkg_repo = dict(pilot.select("name", "repo").iter_rows())
    candidates: dict[str, set[str]] = {}
    cols = classifiers.select("name", "home_page", "download_url", "project_urls")
    for pkg, home, dl, urls in cols.iter_rows():
        repo = pkg_repo.get(pkg)
        if repo is None or repo not in ids:
            continue
        text = " ".join([home or "", dl or "", *(urls or [])])
        for slug in depsdev.github_slugs_in(text):
            if slug.lower() != repo:
                candidates.setdefault(repo, set()).add(slug)
    flat = sorted({slug for slugs in candidates.values() for slug in slugs})
    metas = {m.repo: m for m in GitHubClient(ctx.http, ctx.cache).repos(flat)}
    rows: list[dict[str, Any]] = []
    for repo, slugs in sorted(candidates.items()):
        for slug in sorted(slugs):
            meta = metas.get(slug)
            if meta is None or not meta.found:
                continue
            if meta.database_id == ids[repo]:
                rows.append({"repo": repo, "alias": slug, "until": None})
            elif meta.created_at is not None:
                rows.append({"repo": repo, "alias": slug, "until": meta.created_at})
    return pl.DataFrame(rows, schema={"repo": pl.String, "alias": pl.String, "until": pl.Date})


# ---- stages: build, report ----------------------------------------------------------------
def stage_build(ctx: Context) -> None:
    from depmortality.build import Interim, build
    from depmortality.labels.identity import id_salt

    processed = build(Interim.read(ctx.paths.interim), ctx.settings, id_salt())
    processed.write(ctx.paths.processed)
    log.info(
        "processed: %d packages, %d panel rows", processed.packages.height, processed.panel.height
    )


def stage_report(ctx: Context) -> None:
    from depmortality.reports import gate1, handcheck

    handcheck.write(ctx.paths, ctx.settings, reports_dir())
    gate1.write(ctx.paths, ctx.settings, reports_dir())


STAGES: dict[str, Callable[[Context], None]] = {
    "dryrun": stage_dryrun,
    "universe": stage_universe,
    "resolve": stage_resolve,
    "select": stage_select,
    "extract": stage_extract,
    "pull": stage_pull,
    "build": stage_build,
    "report": stage_report,
}
PILOT = ["universe", "resolve", "select", "extract", "pull", "build", "report"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="depmortality.pipeline", description=__doc__)
    parser.add_argument("stage", choices=[*STAGES, "pilot"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--refresh", action="store_true", help="re-download even if cached")
    mode.add_argument("--cache-only", action="store_true", help="fail on any cache miss")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if creds and not Path(creds).is_absolute():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str((REPO_ROOT / creds).resolve())
    setup_logging()
    settings = load_settings()
    paths = default_paths().ensure()
    cache = DiskCache(
        paths.raw, settings.pilot.snapshot_date, refresh=args.refresh, cache_only=args.cache_only
    )
    ctx = Context(settings, paths, cache, cache_only=args.cache_only, refresh=args.refresh)
    try:
        for stage in PILOT if args.stage == "pilot" else [args.stage]:
            log.info("== stage %s ==", stage)
            STAGES[stage](ctx)
    except QueryRefused as exc:
        log.error("BigQuery guard refused a query: %s", exc)
        return 2
    except CacheMiss as exc:
        log.error("cache-only run hit an uncached response: %s", exc)
        return 3
    except (MissingCredential, MissingSalt) as exc:
        log.error("missing credential: %s", exc)
        return 4
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
