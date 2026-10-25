"""Apply the state machine to a whole panel and return tidy tables."""

from __future__ import annotations

from collections.abc import Mapping

import polars as pl

from depmortality.labels.states import Failure, compute_states


def states_from_panel(
    panel: pl.DataFrame,
    failures: Mapping[str, Failure],
    window: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (states, transitions).

    states: package, month, state_asof, state_final
    transitions: package, from_state, to_state, event_month, observable_month, trigger
    """
    state_rows: dict[str, list[object]] = {
        "package": [],
        "month": [],
        "state_asof": [],
        "state_final": [],
    }
    trans_rows: dict[str, list[object]] = {
        "package": [],
        "from_state": [],
        "to_state": [],
        "event_month": [],
        "observable_month": [],
        "trigger": [],
    }
    for (package,), group in panel.sort("month").group_by(["package"], maintain_order=True):
        months = group["month"].to_list()
        active = group.filter(pl.col("n_maintainer_activity") > 0)["month"].to_list()
        res = compute_states(months[0], months[-1], active, failures.get(str(package)), window)
        for m, a, f in zip(res.months, res.state_asof, res.state_final, strict=True):
            state_rows["package"].append(package)
            state_rows["month"].append(m)
            state_rows["state_asof"].append(a.value)
            state_rows["state_final"].append(f.value)
        for t in res.transitions:
            trans_rows["package"].append(package)
            trans_rows["from_state"].append(t.from_state.value)
            trans_rows["to_state"].append(t.to_state.value)
            trans_rows["event_month"].append(t.event_month)
            trans_rows["observable_month"].append(t.observable_month)
            trans_rows["trigger"].append(t.trigger)
    states = pl.DataFrame(
        state_rows,
        schema={
            "package": pl.String,
            "month": pl.Int32,
            "state_asof": pl.String,
            "state_final": pl.String,
        },
    )
    transitions = pl.DataFrame(
        trans_rows,
        schema={
            "package": pl.String,
            "from_state": pl.String,
            "to_state": pl.String,
            "event_month": pl.Int32,
            "observable_month": pl.Int32,
            "trigger": pl.String,
        },
    )
    return states, transitions
