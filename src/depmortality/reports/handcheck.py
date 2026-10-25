"""reports/gate1_handcheck.csv: 100 seeded pilot packages with their timelines and empty
verdict columns for the owner to fill in by hand.

The file shows bot logins (to judge the bot filter) but never emails, and is git-ignored.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import polars as pl

from depmortality.config import Settings
from depmortality.labels.bots import BotFilter
from depmortality.utils.months import month_label
from depmortality.utils.paths import DataPaths
from depmortality.utils.seeding import rng

VERDICT_COLUMNS = ["verdict_states_ok", "verdict_triggers_ok", "verdict_bots_ok", "verdict_notes"]


def sample_packages(packages: list[str], n: int, seed: int) -> list[str]:
    ordered = sorted(packages)
    idx = rng(seed, "handcheck").choice(len(ordered), size=min(n, len(ordered)), replace=False)
    return [ordered[i] for i in np.sort(idx)]


def activity_string(panel: pl.DataFrame) -> str:
    """'2015-03 c3 r1 m2; ...' for months with any maintainer activity (c=commits,
    r=releases, m=maintainer replies, v=reviews), plus b=bot events removed."""
    parts = []
    for row in panel.filter(
        (pl.col("n_maintainer_activity") > 0) | (pl.col("n_bot_events") > 0)
    ).iter_rows(named=True):
        bits = [
            f"{k}{row[c]}"
            for k, c in (
                ("c", "n_commits"),
                ("r", "n_releases"),
                ("m", "n_replies"),
                ("v", "n_reviews"),
                ("b", "n_bot_events"),
            )
            if row[c]
        ]
        parts.append(f"{month_label(row['month'])} {' '.join(bits)}")
    return "; ".join(parts)


def state_runs(states: pl.DataFrame, column: str) -> str:
    runs: list[str] = []
    start = prev_m = None
    prev_s = None
    for m, s in states.select("month", column).iter_rows():
        if s != prev_s:
            if prev_s is not None:
                runs.append(f"{month_label(start)}..{month_label(prev_m)} {prev_s}")  # type: ignore[arg-type]
            start, prev_s = m, s
        prev_m = m
    if prev_s is not None:
        runs.append(f"{month_label(start)}..{month_label(prev_m)} {prev_s}")  # type: ignore[arg-type]
    return "; ".join(runs)


def write(paths: DataPaths, settings: Settings, out_dir: Path) -> Path:
    proc = paths.processed
    packages = pl.read_parquet(proc / "packages.parquet")
    panel = pl.read_parquet(proc / "panel.parquet")
    states = pl.read_parquet(proc / "states.parquet")
    transitions = pl.read_parquet(proc / "transitions.parquet")
    triggers = pl.read_parquet(proc / "triggers.parquet")
    signals = pl.read_parquet(proc / "signals.parquet")
    bots = BotFilter.from_config(settings.bots)
    gha = pl.read_parquet(paths.interim / "gha_events.parquet")
    commits = pl.read_parquet(paths.interim / "commits.parquet")

    cfg = settings.pilot.handcheck
    chosen = sample_packages(packages["package"].to_list(), cfg.n_packages, cfg.seed)
    out = out_dir / "gate1_handcheck.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "package",
        "repo",
        "pypi_url",
        "github_url",
        "stratum",
        "first_release",
        "activity_by_month",
        "states_asof",
        "states_retrospective",
        "transitions",
        "triggers",
        "inactive_classifier_since",
        "mature_classifier_since",
        "bot_actors_removed",
        *VERDICT_COLUMNS,
    ]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for pkg in chosen:
            info = packages.filter(pl.col("package") == pkg).row(0, named=True)
            st = states.filter(pl.col("package") == pkg).sort("month")
            tr = transitions.filter(pl.col("package") == pkg)
            tg = triggers.filter(pl.col("package") == pkg)
            sg = signals.filter(pl.col("package") == pkg)
            bot_names = sorted(
                {
                    lg
                    for lg in gha.filter(pl.col("package") == pkg)["actor_login"]
                    .drop_nulls()
                    .to_list()
                    if bots.is_bot(login=lg)
                }
                | {
                    n
                    for n, e in commits.filter(pl.col("package") == pkg)
                    .select("author_name", "author_email")
                    .iter_rows()
                    if bots.is_bot(name=n, email=e)
                }
            )
            w.writerow(
                [
                    pkg,
                    info["repo"],
                    f"https://pypi.org/project/{pkg}/",
                    f"https://github.com/{info['repo']}",
                    info["stratum"],
                    month_label(info["origin_month"]),
                    activity_string(panel.filter(pl.col("package") == pkg)),
                    state_runs(st, "state_asof"),
                    state_runs(st, "state_final"),
                    "; ".join(
                        f"{a}->{b} at {month_label(e)} (knowable {month_label(o)})"
                        + (f" [{t}]" if t else "")
                        for a, b, e, o, t in tr.select(
                            "from_state", "to_state", "event_month", "observable_month", "trigger"
                        ).iter_rows()
                    ),
                    "; ".join(
                        f"{k} {d or 'date unknown'}: {x}"
                        for k, d, x in tg.select("kind", "day", "detail").iter_rows()
                    ),
                    sg["inactive_classifier_day"].item() if sg.height else "",
                    sg["mature_classifier_day"].item() if sg.height else "",
                    ", ".join(bot_names),
                    *[""] * len(VERDICT_COLUMNS),
                ]
            )
    return out
