"""The BigQuery guard dry-runs first, caps bytes billed, and refuses unapproved big queries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from depmortality.config import BigQueryThresholds, BqApproval, BqApprovals
from depmortality.utils.bq import GB, GuardedBigQuery, Param, QueryRefused, query_sha256

THRESHOLDS = BigQueryThresholds(
    max_gb_per_query=50, phase_budget_gb=100, min_bytes_billed=10 * 2**20, bytes_billed_headroom=1.1
)


@dataclass
class FakeResult:
    def to_arrow(self, create_bqstorage_client: bool = False) -> pa.Table:
        return pa.table({"x": [1, 2]})


@dataclass
class FakeJob:
    total_bytes_processed: int
    total_bytes_billed: int

    def result(self) -> FakeResult:
        return FakeResult()


@dataclass
class FakeClient:
    estimate: int
    calls: list[dict[str, Any]] = field(default_factory=list)

    def query(self, query: str, job_config: Any = None) -> FakeJob:
        self.calls.append(dict(job_config))
        return FakeJob(self.estimate, 0 if job_config.get("dry_run") else self.estimate)


def guard(
    tmp_path: Path, client: FakeClient, approvals: list[BqApproval] | None = None
) -> GuardedBigQuery:
    return GuardedBigQuery(
        client,
        THRESHOLDS,
        BqApprovals(approvals=approvals or []),
        tmp_path / "ledger.jsonl",
        phase="phase0",
        job_config_factory=lambda **kw: kw,
        params_factory=lambda ps: [(p.name, p.value) for p in ps],
    )


SQL = "SELECT 1 FROM `bigquery-public-data.deps_dev_v1.Snapshots`"


def test_dry_run_happens_first_and_bytes_are_capped(tmp_path: Path) -> None:
    client = FakeClient(estimate=2 * GB)
    table = guard(tmp_path, client).run(SQL, [], "small")
    assert table.num_rows == 2
    assert client.calls[0]["dry_run"] is True
    assert "dry_run" not in client.calls[1]
    assert client.calls[1]["maximum_bytes_billed"] == int(2 * GB * 1.1) or client.calls[1][
        "maximum_bytes_billed"
    ] == pytest.approx(2 * GB * 1.1, abs=1)


def test_over_threshold_query_is_refused(tmp_path: Path) -> None:
    client = FakeClient(estimate=51 * GB)
    with pytest.raises(QueryRefused, match="not approved"):
        guard(tmp_path, client).run(SQL, [], "big")
    assert len(client.calls) == 1 and client.calls[0]["dry_run"] is True  # never executed


def test_approved_query_runs_and_is_capped_at_approval(tmp_path: Path) -> None:
    client = FakeClient(estimate=60 * GB)
    sha = query_sha256(SQL, [])
    ok = BqApproval(query_sha256=sha, max_gb=61, approved_on="2026-10-26")  # type: ignore[arg-type]
    guard(tmp_path, client, [ok]).run(SQL, [], "approved")
    assert client.calls[1]["maximum_bytes_billed"] == 61 * GB


def test_approval_is_pinned_to_exact_query_text(tmp_path: Path) -> None:
    client = FakeClient(estimate=60 * GB)
    other = BqApproval(
        query_sha256=query_sha256(SQL + " WHERE TRUE", []),
        max_gb=100,
        approved_on="2026-10-26",  # type: ignore[arg-type]
    )
    with pytest.raises(QueryRefused):
        guard(tmp_path, client, [other]).run(SQL, [], "mismatch")


def test_params_change_the_hash() -> None:
    assert query_sha256(SQL, [Param("x", "INT64", 1)]) != query_sha256(
        SQL, [Param("x", "INT64", 2)]
    )
    assert query_sha256(SQL, []) == query_sha256("  " + SQL.replace(" ", "\n  "), [])


def test_estimate_above_approved_ceiling_is_refused(tmp_path: Path) -> None:
    client = FakeClient(estimate=70 * GB)
    ok = BqApproval(query_sha256=query_sha256(SQL, []), max_gb=61, approved_on="2026-10-26")  # type: ignore[arg-type]
    with pytest.raises(QueryRefused, match="approved"):
        guard(tmp_path, client, [ok]).run(SQL, [], "grew")


def test_phase_budget_is_cumulative(tmp_path: Path) -> None:
    client = FakeClient(estimate=40 * GB)
    g = guard(tmp_path, client)
    g.run(SQL, [], "one")
    g.run(SQL + " -- two", [], "two")
    with pytest.raises(QueryRefused, match="phase budget"):
        g.run(SQL + " -- three", [], "three")
    ledger = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [e["event"] for e in ledger] == ["dry_run", "run", "dry_run", "run", "dry_run"]


def test_no_client_means_no_query(tmp_path: Path) -> None:
    g = GuardedBigQuery(
        None, THRESHOLDS, BqApprovals(approvals=[]), tmp_path / "l.jsonl", phase="p"
    )
    with pytest.raises(QueryRefused, match="no BigQuery client"):
        g.run(SQL, [], "x")
