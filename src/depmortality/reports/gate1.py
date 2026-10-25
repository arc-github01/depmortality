"""reports/gate1.md and figures. Every number is computed here from pipeline outputs; any
number that cannot be computed is reported as such, never estimated."""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from depmortality.config import Settings
from depmortality.utils.bq import GB
from depmortality.utils.months import month_index
from depmortality.utils.paths import DataPaths

# Single-series charts: one hue, recessive axes (see the dataviz guidance).
SERIES = "#2a78d6"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
KM_TIMES = (12, 24, 36, 60, 96)


@dataclass(frozen=True)
class Durations:
    duration: list[int]
    observed: list[bool]


# ---- survival inputs ------------------------------------------------------------------------
def quiet_to_failed(transitions: pl.DataFrame, packages: pl.DataFrame) -> Durations:
    """From confirmation of each package's first Quiet spell (event_month + W, i.e. its
    observable_month) to failure from Quiet; censored at revival or the end of follow-up."""
    end = dict(packages.select("package", "end_month").iter_rows())
    dur, obs = [], []
    for (pkg,), tr in transitions.sort("observable_month").group_by(
        ["package"], maintain_order=True
    ):
        rows = tr.sort(["observable_month", "event_month"]).to_dicts()
        first = next((i for i, r in enumerate(rows) if r["to_state"] == "QUIET"), None)
        if first is None:
            continue
        start = rows[first]["observable_month"]
        after = rows[first + 1] if first + 1 < len(rows) else None
        if after is not None and after["to_state"] == "FAILED":
            dur.append(after["event_month"] - start)
            obs.append(True)
        elif after is not None and after["to_state"] == "REVIVED":
            dur.append(after["event_month"] - start)
            obs.append(False)
        else:
            dur.append(end[pkg] - start)
            obs.append(False)
    return Durations(dur, obs)


def time_to_first_quiet(
    transitions: pl.DataFrame, packages: pl.DataFrame, window: int
) -> Durations:
    """Origin -> first Quiet (dated at last activity). Censored at Active->Failed, or at the
    last month whose Quiet onset is still confirmable (end - W)."""
    dur, obs = [], []
    by_pkg = {k[0]: g for k, g in transitions.group_by(["package"])}
    for pkg, origin, end in packages.select("package", "origin_month", "end_month").iter_rows():
        tr = by_pkg.get(pkg)
        quiet = tr.filter(pl.col("to_state") == "QUIET") if tr is not None else None
        if quiet is not None and quiet.height:
            dur.append(int(quiet["event_month"].min()) - origin)  # type: ignore[arg-type]
            obs.append(True)
            continue
        failed = tr.filter(pl.col("to_state") == "FAILED") if tr is not None else None
        if failed is not None and failed.height:
            dur.append(int(failed["event_month"].min()) - origin)  # type: ignore[arg-type]
        else:
            dur.append(max(0, end - window - origin))
        obs.append(False)
    return Durations(dur, obs)


def km_summary(d: Durations, title: str, path: Path) -> dict[str, Any]:
    import matplotlib
    from lifelines import KaplanMeierFitter

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not d.duration:
        return {"n": 0}
    kmf = KaplanMeierFitter().fit(d.duration, d.observed)
    sf = kmf.survival_function_.iloc[:, 0]
    ci = kmf.confidence_interval_
    table = kmf.event_table
    at = {}
    for t in KM_TIMES:
        if t <= max(d.duration):
            at[t] = {
                "S": float(kmf.survival_function_at_times(t).iloc[0]),
                "at_risk": int(table.loc[table.index >= t, "at_risk"].iloc[0])
                if (table.index >= t).any()
                else 0,
            }
    # Descriptive plateau check: change in S over the last third of the follow-up during
    # which at least 20 units remain at risk.
    risk = table["at_risk"]
    usable = risk[risk >= 20]
    plateau: dict[str, Any] = {}
    if len(usable) >= 2:
        t_max = float(usable.index.max())
        t_lo = t_max * 2 / 3
        s_lo = float(kmf.survival_function_at_times(t_lo).iloc[0])
        s_hi = float(kmf.survival_function_at_times(t_max).iloc[0])
        plateau = {"from": t_lo, "to": t_max, "S_from": s_lo, "S_to": s_hi, "drop": s_lo - s_hi}

    fig, ax = plt.subplots(figsize=(7, 4), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.fill_between(
        ci.index, ci.iloc[:, 0], ci.iloc[:, 1], step="post", color=SERIES, alpha=0.15, linewidth=0
    )
    ax.step(sf.index, sf.values, where="post", color=SERIES, linewidth=2)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.set_xlabel("months", color=INK_2)
    ax.set_ylabel("survival probability", color=INK_2)
    ax.set_ylim(0, 1.02)
    ax.grid(True, color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return {
        "n": len(d.duration),
        "events": int(sum(d.observed)),
        "max_followup": int(max(d.duration)),
        "at": at,
        "plateau": plateau,
        "median": None
        if math.isinf(kmf.median_survival_time_)
        else float(kmf.median_survival_time_),
    }


# ---- projection -------------------------------------------------------------------------------
def projection(pilot: pl.DataFrame, packages: pl.DataFrame, failed: set[str]) -> dict[str, Any]:
    """Stratified Horvitz–Thompson total of failed packages over the eligible universe,
    with nonresponse handled by using responders' stratum mean (missing at random)."""
    total = 0.0
    var = 0.0
    universe = 0.0
    singletons = 0
    responders = set(packages["package"].to_list())
    for (stratum,), grp in pilot.group_by(["stratum"]):
        n = grp.height
        big_n = float(grp["weight"][0]) * n
        universe += big_n
        resp = [p for p in grp["name"].to_list() if p in responders]
        r = len(resp)
        if r == 0:
            continue
        y = [1.0 if p in failed else 0.0 for p in resp]
        mean = sum(y) / r
        total += big_n * mean
        if r >= 2:
            s2 = sum((v - mean) ** 2 for v in y) / (r - 1)
            var += big_n**2 * max(0.0, 1 - r / big_n) * s2 / r
        else:
            singletons += 1
        _ = stratum
    se = math.sqrt(var)
    return {
        "total": total,
        "se": se,
        "lo": max(0.0, total - 1.96 * se),
        "hi": total + 1.96 * se,
        "universe": universe,
        "singleton_strata": singletons,
    }


# ---- report -----------------------------------------------------------------------------------
def _ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _n(x: object) -> str:
    return f"{x:,}" if isinstance(x, int) else "n/a"


def _fmt(x: float | None, nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def write(paths: DataPaths, settings: Settings, out_dir: Path) -> Path:
    proc, interim = paths.processed, paths.interim
    packages = pl.read_parquet(proc / "packages.parquet")
    transitions = pl.read_parquet(proc / "transitions.parquet")
    triggers = pl.read_parquet(proc / "triggers.parquet")
    signals = pl.read_parquet(proc / "signals.parquet")
    pilot = pl.read_parquet(interim / "pilot.parquet")
    status = pl.read_parquet(interim / "pull_status.parquet")
    pilot_meta = json.loads((interim / "pilot_meta.json").read_text())
    universe_meta = json.loads((interim / "universe_meta.json").read_text())
    window = settings.thresholds.states.quiet_window_months
    g1 = settings.thresholds.gate1
    sp = settings.splits
    figs = out_dir / "figures"
    unavailable: list[str] = []

    # pull outcomes
    n_pilot = pilot.height
    n_ok = packages.height
    fail_rate = 1 - n_ok / n_pilot if n_pilot else 1.0
    reasons = (
        status.filter(~pl.col("ok"))
        .with_columns(pl.col("reason").str.slice(0, 90))
        .group_by("step", "reason")
        .len()
        .sort("len", descending=True)
    )

    # transitions and triggers
    to = pl.col("to_state")
    quiet = transitions.filter(to == "QUIET")
    revived = transitions.filter(to == "REVIVED")
    failed_tr = transitions.filter(to == "FAILED")
    failed = set(failed_tr["package"].to_list())
    by_trigger = failed_tr.group_by("trigger", "from_state").len().sort("trigger", "from_state")
    fired = (
        triggers.group_by("kind")
        .agg(pl.len().alias("fired"), pl.col("day").is_null().sum().alias("undated"))
        .sort("kind")
    )

    def period(col: str) -> pl.Expr:
        return (
            pl.when(pl.col(col) <= month_index(sp.train_end))
            .then(pl.lit("train"))
            .when(pl.col(col) <= month_index(sp.calibration_end))
            .then(pl.lit("calibration"))
            .otherwise(pl.lit("test"))
        )

    knowable = (
        transitions.with_columns(period("observable_month").alias("period"))
        .group_by("to_state", "period")
        .len()
        .sort("to_state", "period")
    )

    inactive = set(
        signals.filter(pl.col("inactive_classifier_day").is_not_null())["package"].to_list()
    )
    mature = set(signals.filter(pl.col("mature_classifier_day").is_not_null())["package"].to_list())
    quiet_pkgs = set(quiet["package"].to_list())
    monorepo = packages.filter(pl.col("packages_in_repo") > 1).height
    post_fail = packages.filter(pl.col("post_failure_activity") > 0).height

    km_qf = km_summary(
        quiet_to_failed(transitions, packages),
        "Quiet → Failed (months since Quiet confirmed), pilot, unweighted",
        figs / "gate1_km_quiet_to_failed.png",
    )
    km_q = km_summary(
        time_to_first_quiet(transitions, packages, window),
        "Time to first Quiet (months since first release), pilot, unweighted",
        figs / "gate1_km_time_to_quiet.png",
    )

    proj = projection(pilot, packages, failed)
    eligible = pilot_meta.get("eligible_packages")

    bq = [e for e in _ledger(paths.bq_ledger) if e.get("phase") == "phase0"]
    billed = sum(int(e.get("bytes_billed") or 0) for e in bq if e.get("event") == "run")
    n_runs = sum(1 for e in bq if e.get("event") == "run")
    n_dry = sum(1 for e in bq if e.get("event") == "dry_run")
    api: dict[str, int] = {}
    for e in _ledger(paths.api_ledger):
        if e.get("phase") == "phase0":
            for host, n in e["calls"].items():
                api[host] = api.get(host, 0) + int(n)

    # verdict
    reasons_v: list[str] = []
    if proj["total"] >= g1.min_projected_failures:
        verdict = "viable"
        reasons_v.append(f"projected failures {proj['total']:,.0f} ≥ {g1.min_projected_failures:,}")
    elif proj["total"] >= g1.risky_projected_failures:
        verdict = "risky"
        reasons_v.append(
            f"projected failures {proj['total']:,.0f} between {g1.risky_projected_failures:,} and {g1.min_projected_failures:,}"
        )
    else:
        verdict = "not viable"
        reasons_v.append(
            f"projected failures {proj['total']:,.0f} < {g1.risky_projected_failures:,}"
        )
    if proj["lo"] < g1.min_projected_failures <= proj["total"]:
        reasons_v.append(
            f"the 95% interval's lower end ({proj['lo']:,.0f}) is below {g1.min_projected_failures:,}"
        )
        verdict = "risky" if verdict == "viable" else verdict
    if fail_rate > g1.max_pull_failure_rate:
        reasons_v.append(f"pull failure rate {fail_rate:.1%} > {g1.max_pull_failure_rate:.0%}")
        verdict = "risky" if verdict == "viable" else verdict
    plateau = km_qf.get("plateau") or {}
    if plateau:
        reasons_v.append(
            f"Quiet→Failed KM fell {plateau['drop']:.3f} between month {plateau['from']:.0f} and "
            f"{plateau['to']:.0f} after confirmation (≥20 at risk) — descriptive only; formal test in Phase 1"
        )
    else:
        unavailable.append("Quiet→Failed plateau check: fewer than two KM times with ≥ 20 at risk")

    lines: list[str] = []
    add = lines.append
    add("# Gate 1 report — Phase 0 pilot")
    add("")
    add(
        f"Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC by `pipeline report` from pipeline outputs only. "
        f"deps.dev snapshot {universe_meta.get('depsdev_snapshot', 'n/a')}; data freeze {sp.data_freeze}; "
        f"quiet window {window} months."
    )
    add("")
    add(f"## Verdict: **{verdict}** (provisional — pending the 100-package hand check)")
    add("")
    for r in reasons_v:
        add(f"- {r}")
    add(
        "- The hand check (`reports/gate1_handcheck.csv`) has not been scored yet; Gate 1 also "
        "requires the bot filter and state machine to pass it."
    )
    add("")
    add("## Pilot and pulls")
    add("")
    add(
        f"- Universe (≥1 direct dependent, GitHub repo, first upload {settings.pilot.cohort_first_release_start}–{settings.pilot.cohort_first_release_end}): "
        f"{_n(universe_meta.get('packages'))} packages; eligible after repo resolution: {_n(eligible)}."
    )
    add(
        f"- Pilot selected: {n_pilot:,} ({pilot.filter(pl.col('selected_as') == 'top').height:,} top, "
        f"{pilot.filter(pl.col('selected_as') == 'tail').height:,} tail)."
    )
    add(
        f"- Pulled successfully (PyPI + git, in the analysis): **{n_ok:,}**; not analysed: {n_pilot - n_ok:,} ({fail_rate:.1%})."
    )
    add(f"- Packages sharing a repo with another pilot package (monorepo flag): {monorepo:,}.")
    add("")
    if reasons.height:
        add("| step | reason | packages |")
        add("|---|---|---:|")
        for step, reason, n in reasons.head(25).iter_rows():
            add(f"| {step} | {str(reason).replace('|', '/')} | {n:,} |")
        add("")
    add("## Transitions")
    add("")
    add(f"- Quiet transitions: {quiet.height:,} across {len(quiet_pkgs):,} packages.")
    add(f"- Revivals: {revived.height:,} across {revived['package'].n_unique():,} packages.")
    add(f"- Failed (first dated trigger): **{failed_tr.height:,}** packages.")
    add(
        f"- Packages with maintainer activity after their failure date: {post_fail:,} (activity is ignored after failure)."
    )
    add("")
    add("| failure trigger | from state | packages |")
    add("|---|---|---:|")
    for trig, frm, n in by_trigger.iter_rows():
        add(f"| {trig} | {frm} | {n:,} |")
    add("")
    add(
        "All trigger firings (a package can fire several; undated ones cannot be placed on the timeline):"
    )
    add("")
    add("| trigger | fired | undated |")
    add("|---|---:|---:|")
    for kind, n, und in fired.iter_rows():
        add(f"| {kind} | {n:,} | {und:,} |")
    add("")
    add("- Trigger (d), explicit statements, is not implemented in Phase 0.")
    add("")
    add("When each transition becomes knowable (by `observable_month`, splits from config):")
    add("")
    add("| transition to | period | count |")
    add("|---|---|---:|")
    for st, per, n in knowable.iter_rows():
        add(f"| {st} | {per} | {n:,} |")
    add("")
    add("## Classifier signals")
    add("")
    add(
        f"- `7 - Inactive`: {len(inactive):,} packages; of these {len(inactive & failed):,} failed and {len(inactive & quiet_pkgs):,} went Quiet. "
        f"Of {len(failed):,} failed packages, {len(failed & inactive):,} carry Inactive."
    )
    add(
        f"- `6 - Mature` (validation only): {len(mature):,} packages; {len(mature & failed):,} failed, {len(mature & quiet_pkgs):,} went Quiet."
    )
    add("")
    add("## Kaplan–Meier curves (pilot, unweighted)")
    add("")
    for name, km, fig in (
        ("Quiet → Failed, from Quiet confirmation", km_qf, "gate1_km_quiet_to_failed.png"),
        ("Time to first Quiet, from first release", km_q, "gate1_km_time_to_quiet.png"),
    ):
        add(f"### {name}")
        add("")
        if not km.get("n"):
            add("Could not be computed: no units.")
            add("")
            continue
        add(f"![{name}](figures/{fig})")
        add("")
        add(
            f"n = {km['n']:,}, events = {km['events']:,}, max follow-up = {km['max_followup']} months, "
            f"median = {_fmt(km['median'], 1)} months."
        )
        add("")
        add("| month | S(t) | at risk |")
        add("|---:|---:|---:|")
        for t, v in km["at"].items():
            add(f"| {t} | {v['S']:.3f} | {v['at_risk']:,} |")
        add("")
    add(
        "Note: Quiet → Failed time is measured from Quiet *confirmation* (last activity + W). "
        "Failures inside the W-month window are, by the state-machine rule, Active → Failed, so "
        "measuring from the last activity would put a structural zero in the first W months."
    )
    add("")
    add("## Projection to the eligible universe")
    add("")
    add(
        f"- Projected failed packages: **{proj['total']:,.0f}** (95% interval {proj['lo']:,.0f}–{proj['hi']:,.0f}, SE {proj['se']:,.0f}) "
        f"over a design-weighted universe of {proj['universe']:,.0f} packages."
    )
    add(
        "- Assumptions: stratified Horvitz–Thompson estimator with the pilot's selection weights; "
        "non-responding packages resemble responders in their stratum; the eligible universe is "
        "today's (selection on current dependents, a survivor bias logged in DECISIONS.md); "
        "the failure definition is Phase 0's (triggers a–c only, undated triggers excluded). "
        f"Strata with a single responder contribute no variance ({proj['singleton_strata']})."
    )
    add("")
    add("## Cost")
    add("")
    add(
        f"- BigQuery: {n_runs} queries run, {billed / GB:,.3f} GB billed in phase0; {n_dry} dry-runs (free)."
    )
    if api:
        add(
            "- API calls (network only; cache hits excluded): "
            + ", ".join(f"{h}: {n:,}" for h, n in sorted(api.items()))
            + "."
        )
    else:
        unavailable.append("API call counts: no api_ledger entries")
    add("")
    if unavailable:
        add("## Not computed")
        add("")
        for u in unavailable:
            add(f"- {u}")
        add("")
    out = out_dir / "gate1.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
