"""Default-branch commit history from commit-only git clones (DECISIONS.md 2026-10-26).

``git clone --bare --filter=tree:0 --single-branch`` fetches commits but no trees or blobs,
so even large repositories are small. The parsed log text is stored in the DiskCache, so a
cache-only rebuild never needs the clones.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from depmortality.utils.cache import DiskCache

FIELD = "\x1f"
RECORD = "\x1e"
LOG_FORMAT = FIELD.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI"]) + RECORD
FAILED_MARKER = "__CLONE_FAILED__:"
COMMIT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "sha": pl.String,
    "n_parents": pl.Int64,
    "author_name": pl.String,
    "author_email": pl.String,
    "author_day": pl.Date,
    "committer_name": pl.String,
    "committer_email": pl.String,
    "committer_day": pl.Date,
}
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GCM_INTERACTIVE": "never"}


class CloneFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    n_parents: int
    author_name: str
    author_email: str
    author_day: dt.date
    committer_name: str
    committer_email: str
    committer_day: dt.date


def _day(text: str) -> dt.date:
    return dt.datetime.fromisoformat(text).astimezone(dt.UTC).date()


def parse_log(text: str) -> list[Commit]:
    commits = []
    for record in text.split(RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, an, ae, ad, cn, ce, cd = record.split(FIELD)
        commits.append(
            Commit(
                sha=sha,
                n_parents=len(parents.split()) if parents else 0,
                author_name=an,
                author_email=ae,
                author_day=_day(ad),
                committer_name=cn,
                committer_email=ce,
                committer_day=_day(cd),
            )
        )
    return commits


def parse_log_frame(text: str) -> pl.DataFrame:
    """Columnar ``parse_log``: millions of commits fit in memory this way."""
    records = [r.strip("\n") for r in text.split(RECORD)]
    rows = [r.split(FIELD) for r in records if r]
    if not rows:
        return pl.DataFrame(schema=COMMIT_SCHEMA)
    cols = list(zip(*rows, strict=True))

    def day(values: tuple[str, ...]) -> pl.Series:
        # git writes UTC offsets as either "+00:00" or "Z".
        return (
            pl.Series(values)
            .str.replace(r"Z$", "+00:00")
            .str.to_datetime("%Y-%m-%dT%H:%M:%S%:z", time_zone="UTC")
            .dt.date()
        )

    return pl.DataFrame(
        {
            "sha": cols[0],
            "n_parents": [len(p.split()) for p in cols[1]],
            "author_name": cols[2],
            "author_email": cols[3],
            "author_day": day(cols[4]),
            "committer_name": cols[5],
            "committer_email": cols[6],
            "committer_day": day(cols[7]),
        },
        schema=COMMIT_SCHEMA,
    )


def _git(*args: str, cwd: Path | None = None, timeout: float = 1800) -> str:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise CloneFailed(
            proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "git failed"
        )
    return proc.stdout


def clone_log(repo: str, mirrors: Path, *, keep_mirror: bool = False) -> str:
    """Clone github.com/<repo> commit-only and return the default-branch log text."""
    dest = mirrors / repo.replace("/", "__")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    url = f"https://github.com/{repo}.git"
    try:
        _git(
            "clone",
            "--bare",
            "--filter=tree:0",
            "--single-branch",
            "--no-tags",
            "--quiet",
            url,
            str(dest),
        )
        return _git("log", "HEAD", f"--format={LOG_FORMAT}", cwd=dest)
    finally:
        if not keep_mirror:
            shutil.rmtree(dest, ignore_errors=True)


def _log_text(repo: str, cache: DiskCache, mirrors: Path) -> str:
    """Cached: the raw log text is stored under source 'git'."""

    def fetch() -> bytes:
        # Failures are cached too, so a cache-only rebuild reproduces them.
        try:
            return clone_log(repo, mirrors).encode("utf-8")
        except (CloneFailed, subprocess.TimeoutExpired) as exc:
            return (FAILED_MARKER + str(exc)).encode("utf-8")

    text = cache.get_bytes("git", {"repo": repo}, fetch, suffix="log").decode("utf-8")
    if text.startswith(FAILED_MARKER):
        raise CloneFailed(text.removeprefix(FAILED_MARKER))
    return text


def commits_for(repo: str, cache: DiskCache, mirrors: Path) -> list[Commit]:
    return parse_log(_log_text(repo, cache, mirrors))


def commits_frame(repo: str, cache: DiskCache, mirrors: Path) -> pl.DataFrame:
    return parse_log_frame(_log_text(repo, cache, mirrors))
