"""BigQuery wrapper that enforces the cost guardrails in CLAUDE.md rule 7.

Every query is dry-run first and its estimated bytes logged. Every job gets
``maximum_bytes_billed``. A query estimated above ``max_gb_per_query`` runs only if its
exact text (by hash) is listed in config/bq_approvals.yaml. The cumulative bytes billed in
a phase may not exceed the phase budget. Every dry-run and run is appended to a ledger.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from depmortality.config import BigQueryThresholds, BqApprovals
from depmortality.utils.cache import DiskCache, canonical_json

log = logging.getLogger(__name__)

GB = 1_000_000_000  # decimal GB: the conservative reading of "50 GB"


class QueryRefused(RuntimeError):
    pass


class BigQueryLike(Protocol):
    def query(self, query: str, job_config: Any = ...) -> Any: ...


@dataclass(frozen=True)
class Param:
    """A query parameter. ``kind`` is a BigQuery type, prefixed ``ARRAY<`` for arrays."""

    name: str
    kind: str
    value: Any


@dataclass(frozen=True)
class DryRun:
    label: str
    sql_sha256: str
    bytes_processed: int

    @property
    def gb(self) -> float:
        return self.bytes_processed / GB


def normalise_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def query_sha256(sql: str, params: list[Param]) -> str:
    material = canonical_json({"sql": normalise_sql(sql), "params": [asdict(p) for p in params]})
    return hashlib.sha256(material.encode()).hexdigest()


def _to_bq_params(params: list[Param]) -> list[Any]:
    from google.cloud import bigquery

    out: list[Any] = []
    for p in params:
        if p.kind.startswith("ARRAY<") and p.kind.endswith(">"):
            out.append(bigquery.ArrayQueryParameter(p.name, p.kind[6:-1], list(p.value)))
        else:
            out.append(bigquery.ScalarQueryParameter(p.name, p.kind, p.value))
    return out


def _job_config(**kwargs: Any) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(**kwargs)


class GuardedBigQuery:
    def __init__(
        self,
        client: BigQueryLike | None,
        thresholds: BigQueryThresholds,
        approvals: BqApprovals,
        ledger_path: Path,
        *,
        phase: str,
        cache: DiskCache | None = None,
        job_config_factory: Any = _job_config,
        params_factory: Any = _to_bq_params,
    ) -> None:
        self.client = client
        self.t = thresholds
        self.approvals = {a.query_sha256: a for a in approvals.approvals}
        self.phase_budget_bytes = int(
            (approvals.phase_budget_override_gb or thresholds.phase_budget_gb) * GB
        )
        self.ledger_path = ledger_path
        self.phase = phase
        self.cache = cache
        self._job_config = job_config_factory
        self._params = params_factory

    # ---- ledger -------------------------------------------------------------------------
    def _append_ledger(self, entry: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": dt.datetime.now(dt.UTC).isoformat(), "phase": self.phase, **entry}
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def spent_bytes(self) -> int:
        if not self.ledger_path.exists():
            return 0
        total = 0
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("phase") == self.phase and entry.get("event") == "run":
                total += int(entry.get("bytes_billed") or 0)
        return total

    # ---- guard --------------------------------------------------------------------------
    def _require_client(self) -> BigQueryLike:
        if self.client is None:
            raise QueryRefused("no BigQuery client (credentials missing or cache-only mode)")
        return self.client

    def dry_run(self, sql: str, params: list[Param], label: str) -> DryRun:
        client = self._require_client()
        cfg = self._job_config(
            dry_run=True, use_query_cache=False, query_parameters=self._params(params)
        )
        job = client.query(sql, job_config=cfg)
        bytes_processed = int(job.total_bytes_processed or 0)
        dry = DryRun(label, query_sha256(sql, params), bytes_processed)
        log.info("dry-run %s: %.3f GB (sha256 %s)", label, dry.gb, dry.sql_sha256[:12])
        self._append_ledger(
            {"event": "dry_run", "label": label, "sha256": dry.sql_sha256, "bytes": bytes_processed}
        )
        return dry

    def maximum_bytes_billed(self, dry: DryRun) -> int:
        """Raises QueryRefused unless the query is within the guardrails."""
        approval = self.approvals.get(dry.sql_sha256)
        over_cap = dry.bytes_processed > self.t.max_gb_per_query * GB
        if over_cap and approval is None:
            raise QueryRefused(
                f"{dry.label}: estimated {dry.gb:.2f} GB exceeds {self.t.max_gb_per_query} GB "
                f"and is not approved (sha256 {dry.sql_sha256}). Add it to bq_approvals.yaml."
            )
        if approval is not None and dry.bytes_processed > approval.max_gb * GB:
            raise QueryRefused(
                f"{dry.label}: estimated {dry.gb:.2f} GB exceeds its approved {approval.max_gb} GB"
            )
        spent = self.spent_bytes()
        if spent + dry.bytes_processed > self.phase_budget_bytes:
            raise QueryRefused(
                f"{dry.label}: phase '{self.phase}' has billed {spent / GB:.2f} GB; this query "
                f"({dry.gb:.2f} GB) would exceed the phase budget "
                f"{self.phase_budget_bytes / GB:.2f} GB"
            )
        estimate = max(self.t.min_bytes_billed, dry.bytes_processed)
        limit = math.ceil(estimate * self.t.bytes_billed_headroom)
        if approval is not None:
            limit = min(limit, int(approval.max_gb * GB))
        return limit

    # ---- run ----------------------------------------------------------------------------
    def _execute(
        self, sql: str, params: list[Param], label: str, destination: str | None
    ) -> pa.Table:
        client = self._require_client()
        dry = self.dry_run(sql, params, label)
        limit = self.maximum_bytes_billed(dry)
        extra: dict[str, Any] = {}
        if destination is not None:
            extra = {"destination": destination, "write_disposition": "WRITE_TRUNCATE"}
        cfg = self._job_config(
            maximum_bytes_billed=limit, query_parameters=self._params(params), **extra
        )
        job = client.query(sql, job_config=cfg)
        result = job.result()
        billed = int(job.total_bytes_billed or 0)
        self._append_ledger(
            {
                "event": "run",
                "label": label,
                "sha256": dry.sql_sha256,
                "bytes_estimated": dry.bytes_processed,
                "bytes_billed": billed,
                "maximum_bytes_billed": limit,
                "destination": destination,
            }
        )
        log.info("ran %s: billed %.3f GB", label, billed / GB)
        if destination is not None:
            return pa.table({})
        table: pa.Table = result.to_arrow(create_bqstorage_client=False)
        return table

    def run(
        self,
        sql: str,
        params: list[Param],
        label: str,
        *,
        destination: str | None = None,
    ) -> pa.Table:
        """Run a guarded query. Results (not destination writes) are cached as Parquet."""
        if self.cache is None or destination is not None:
            return self._execute(sql, params, label, destination)
        key = {"sql": normalise_sql(sql), "params": [asdict(p) for p in params]}

        def fetch() -> bytes:
            buf = io.BytesIO()
            pq.write_table(self._execute(sql, params, label, None), buf)
            return buf.getvalue()

        data = self.cache.get_bytes("bigquery", key, fetch, suffix="parquet")
        return pq.read_table(io.BytesIO(data))


def make_client() -> BigQueryLike:
    """A real client from GOOGLE_CLOUD_PROJECT / GOOGLE_APPLICATION_CREDENTIALS."""
    import os

    from google.cloud import bigquery

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise QueryRefused("GOOGLE_CLOUD_PROJECT is not set (environment or .env)")
    client: BigQueryLike = bigquery.Client(project=project)
    return client
