"""State machine on hand-built synthetic timelines (W = 12 unless stated)."""

from __future__ import annotations

from depmortality.config import Settings
from depmortality.labels.states import Failure, State, StateResult, Transition, compute_states

A, Q, R, F = State.ACTIVE, State.QUIET, State.REVIVED, State.FAILED
W = 12
O = 100  # origin month index; keeps arithmetic readable


def at(res: StateResult, view: str, month: int) -> State:
    states: list[State] = getattr(res, view)
    return states[month - res.origin]


def test_never_active_after_first_release() -> None:
    # Only the first release (the origin) ever happens.
    res = compute_states(O, O + 30, [], None, W)
    assert res.transitions == [Transition(A, Q, O, O + W)]
    # as_of: still Active until the window completes, Quiet from then on.
    assert all(at(res, "state_asof", m) == A for m in range(O, O + W))
    assert all(at(res, "state_asof", m) == Q for m in range(O + W, O + 31))
    # final: Quiet from the month after the last activity.
    assert at(res, "state_final", O) == A
    assert all(at(res, "state_final", m) == Q for m in range(O + 1, O + 31))


def test_active_then_quiet_dated_at_last_activity() -> None:
    acts = [O, O + 3, O + 10, O + 20]
    res = compute_states(O, O + 60, acts, None, W)
    assert res.transitions == [Transition(A, Q, O + 20, O + 32)]
    assert at(res, "state_asof", O + 31) == A
    assert at(res, "state_asof", O + 32) == Q
    assert at(res, "state_final", O + 21) == Q


def test_gap_shorter_than_window_is_not_quiet() -> None:
    # Activity at O and O + 12: 11 empty months in between, so no Quiet spell.
    res = compute_states(O, O + 20, [O, O + 12], None, W)
    assert res.transitions == []
    assert set(res.state_asof) == {A}


def test_gap_of_exactly_window_is_quiet() -> None:
    # 12 empty months (O+1..O+12), activity at O+13: Quiet confirmed at O+12, revived at O+13.
    res = compute_states(O, O + 20, [O, O + 13], None, W)
    assert res.transitions == [Transition(A, Q, O, O + 12), Transition(Q, R, O + 13, O + 13)]
    assert at(res, "state_asof", O + 12) == Q
    assert at(res, "state_asof", O + 13) == R


def test_quiet_then_revived_stays_revived() -> None:
    # Ends at O + 36, the month before the revived spell's own Quiet would be confirmed.
    res = compute_states(O, O + 36, [O, O + 20, O + 25], None, W)
    assert res.transitions == [Transition(A, Q, O, O + W), Transition(Q, R, O + 20, O + 20)]
    assert at(res, "state_final", O + 19) == Q
    assert all(at(res, "state_asof", m) == R for m in range(O + 20, O + 37))


def test_archived_while_active() -> None:
    res = compute_states(O, O + 40, [O, O + 2, O + 5], Failure(O + 8, "archived"), W)
    assert res.transitions == [Transition(A, F, O + 8, O + 8, "archived")]
    assert at(res, "state_asof", O + 7) == A
    assert all(at(res, "state_asof", m) == F for m in range(O + 8, O + 41))
    assert res.state_final == res.state_asof


def test_failure_before_quiet_is_confirmed_goes_active_to_failed() -> None:
    # Last activity O+5; Quiet would be confirmed at O+17, but failure lands at O+17.
    res = compute_states(O, O + 40, [O, O + 5], Failure(O + 17, "advisory"), W)
    assert res.transitions == [Transition(A, F, O + 17, O + 17, "advisory")]


def test_failure_while_quiet() -> None:
    res = compute_states(O, O + 60, [O, O + 5], Failure(O + 40, "python_support"), W)
    assert res.transitions == [
        Transition(A, Q, O + 5, O + 17),
        Transition(Q, F, O + 40, O + 40, "python_support"),
    ]
    assert at(res, "state_final", O + 39) == Q


def test_activity_after_failure_is_ignored() -> None:
    res = compute_states(O, O + 40, [O, O + 30], Failure(O + 10, "archived"), W)
    assert res.transitions == [Transition(A, F, O + 10, O + 10, "archived")]
    assert at(res, "state_asof", O + 30) == F


def test_multiple_quiet_spells() -> None:
    acts = [O, O + 20, O + 50, O + 51]
    res = compute_states(O, O + 80, acts, None, W)
    assert res.transitions == [
        Transition(A, Q, O, O + 12),
        Transition(Q, R, O + 20, O + 20),
        Transition(R, Q, O + 20, O + 32),
        Transition(Q, R, O + 50, O + 50),
        Transition(R, Q, O + 51, O + 63),
    ]
    assert at(res, "state_asof", O + 31) == R
    assert at(res, "state_asof", O + 32) == Q


def test_trailing_gap_shorter_than_window_is_censored_not_quiet() -> None:
    res = compute_states(O, O + 30, [O, O + 25], None, W)
    assert res.transitions == [Transition(A, Q, O, O + 12), Transition(Q, R, O + 25, O + 25)]
    assert at(res, "state_final", O + 30) == R


def test_failure_dated_before_origin_is_clamped() -> None:
    res = compute_states(O, O + 10, [O], Failure(O - 5, "archived"), W)
    assert res.transitions == [Transition(A, F, O, O, "archived")]


def test_window_from_config(settings: Settings) -> None:
    w = settings.thresholds.states.quiet_window_months
    res = compute_states(O, O + 3 * w, [O], None, w)
    assert res.transitions[0].observable_month == O + w
