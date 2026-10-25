"""Active / Quiet / Revived / Failed state machine (CLAUDE.md section 3).

Months are integer indices (see utils.months). Inputs are months with at least one
maintainer activity, already bot-filtered. With window W:

- The package is Quiet once W consecutive months pass with no activity after its last
  activity L. The transition is *dated* at L (event_month = L) but only *knowable* at
  L + W (observable_month). The time origin counts as activity (a first release is one).
- Activity at R after a Quiet spell (R >= L + W + 1) is a revival: Quiet -> Revived. The
  package stays Revived (an active spell after a quiet one) until it goes quiet again.
- Failed is absorbing, from the month of the first dated failure trigger. A Quiet spell is
  recognised only if it was confirmed before that month (L + W < F); otherwise the
  transition is Active/Revived -> Failed.

Two views are produced:
- ``state_asof[M]`` uses only data up to M. It never changes when later data is added.
- ``state_final[M]`` is the retrospective label using everything up to the end month
  (Quiet from L + 1). It is for display and hand-checks, never for features.

Dataset builders must keep only transitions with observable_month <= their cutoff.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    ACTIVE = "ACTIVE"
    QUIET = "QUIET"
    REVIVED = "REVIVED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Transition:
    from_state: State
    to_state: State
    event_month: int
    observable_month: int
    trigger: str | None = None


@dataclass(frozen=True)
class Failure:
    month: int
    trigger: str


@dataclass(frozen=True)
class StateResult:
    origin: int
    end: int
    state_asof: list[State]
    state_final: list[State]
    transitions: list[Transition]

    @property
    def months(self) -> range:
        return range(self.origin, self.end + 1)


def compute_states(
    origin: int,
    end: int,
    activity_months: Iterable[int],
    failure: Failure | None,
    window: int,
) -> StateResult:
    if end < origin:
        raise ValueError(f"end month {end} precedes origin {origin}")
    if window < 1:
        raise ValueError("window must be >= 1")
    fail_month = None
    if failure is not None and failure.month <= end:
        fail_month = max(failure.month, origin)
    horizon = end if fail_month is None else min(end, fail_month - 1)
    acts = sorted({m for m in activity_months if origin <= m <= horizon} | {origin})

    # ---- as_of view: a forward pass that only ever looks at months <= M -------------------
    asof: list[State] = []
    act_set = set(acts)
    last = origin
    ever_quiet = False
    state = State.ACTIVE
    for m in range(origin, end + 1):
        if fail_month is not None and m >= fail_month:
            asof.append(State.FAILED)
            continue
        if m in act_set:
            last = m
            if state == State.QUIET:
                state = State.REVIVED
        elif m - last >= window:
            state = State.QUIET
            ever_quiet = True
        if state != State.QUIET:
            state = State.REVIVED if ever_quiet else State.ACTIVE
        asof.append(state)

    # ---- retrospective view and transitions ----------------------------------------------
    final = list(asof)
    transitions: list[Transition] = []
    current = State.ACTIVE
    for i, a in enumerate(acts):
        nxt = acts[i + 1] if i + 1 < len(acts) else None
        # The gap after activity a runs until the next activity, the failure, or past the end.
        gap_end = nxt if nxt is not None else (fail_month if fail_month is not None else end + 1)
        confirmed = a + window
        if confirmed >= gap_end:
            continue
        transitions.append(Transition(current, State.QUIET, a, confirmed))
        for m in range(a + 1, min(gap_end, end + 1)):
            final[m - origin] = State.QUIET
        if nxt is not None:
            transitions.append(Transition(State.QUIET, State.REVIVED, nxt, nxt))
            current = State.REVIVED
        else:
            current = State.QUIET
    if fail_month is not None and failure is not None:
        transitions.append(
            Transition(current, State.FAILED, fail_month, fail_month, failure.trigger)
        )
    return StateResult(origin, end, asof, final, transitions)
