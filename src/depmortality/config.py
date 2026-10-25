"""Typed configuration loaded from the YAML files in config/.

No thresholds, dates or seeds are hard-coded elsewhere; everything comes through here.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Splits(_Strict):
    train_end: dt.date
    calibration_start: dt.date
    calibration_end: dt.date
    test_start: dt.date
    data_freeze: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> Splits:
        order = [
            self.train_end,
            self.calibration_start,
            self.calibration_end,
            self.test_start,
            self.data_freeze,
        ]
        if order != sorted(order) or self.train_end >= self.calibration_start:
            raise ValueError(f"split dates out of order: {order}")
        return self


class StateThresholds(_Strict):
    quiet_window_months: int = Field(ge=1)
    primary_maintainer_window_months: int = Field(ge=1)


class TriggerThresholds(_Strict):
    advisory_window_days: int = Field(ge=1)
    python_support_lag_months: int = Field(ge=0)
    python_support_requires_py3_evidence: bool


class BigQueryThresholds(_Strict):
    max_gb_per_query: float = Field(gt=0)
    phase_budget_gb: float = Field(gt=0)
    min_bytes_billed: int = Field(ge=0)
    bytes_billed_headroom: float = Field(ge=1.0)


class HttpThresholds(_Strict):
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    backoff_base_seconds: float = Field(ge=0)
    backoff_max_seconds: float = Field(ge=0)
    github_min_remaining: int = Field(ge=0)
    pypi_concurrency: int = Field(ge=1)


class Gate1Thresholds(_Strict):
    min_projected_failures: int = Field(ge=0)
    risky_projected_failures: int = Field(ge=0)
    max_pull_failure_rate: float = Field(ge=0, le=1)


class Thresholds(_Strict):
    states: StateThresholds
    triggers: TriggerThresholds
    bigquery: BigQueryThresholds
    http: HttpThresholds
    gate1: Gate1Thresholds


class Bots(_Strict):
    logins: list[str]
    names: list[str]
    email_fragments: list[str]
    heuristic_tokens: list[str]


class HandcheckConfig(_Strict):
    n_packages: int = Field(ge=1)
    seed: int


class PilotConfig(_Strict):
    snapshot_date: dt.date
    seed: int
    n_top: int = Field(ge=0)
    n_tail: int = Field(ge=0)
    cohort_first_release_start: dt.date
    cohort_first_release_end: dt.date
    tail_dependent_bin_edges: list[int]
    tail_allocation: Literal["proportional"]
    handcheck: HandcheckConfig

    @model_validator(mode="after")
    def _edges_sorted(self) -> PilotConfig:
        edges = self.tail_dependent_bin_edges
        if not edges or edges != sorted(set(edges)) or edges[0] < 1:
            raise ValueError("tail_dependent_bin_edges must be strictly increasing and >= 1")
        return self


class PythonReleases(_Strict):
    source: str
    retrieved: dt.date
    releases: dict[str, dt.date]


class BqApproval(_Strict):
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_gb: float = Field(gt=0)
    approved_on: dt.date
    note: str = ""


class BqApprovals(_Strict):
    approvals: list[BqApproval]
    phase_budget_override_gb: float | None = None


class Settings(_Strict):
    config_dir: Path
    splits: Splits
    thresholds: Thresholds
    bots: Bots
    pilot: PilotConfig
    python_releases: PythonReleases
    bq_approvals: BqApprovals


def _read_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_settings(config_dir: Path | None = None) -> Settings:
    """Load and validate every config file. Raises on unknown or missing keys."""
    root = config_dir or DEFAULT_CONFIG_DIR
    return Settings.model_validate(
        {
            "config_dir": root,
            "splits": _read_yaml(root / "splits.yaml"),
            "thresholds": _read_yaml(root / "thresholds.yaml"),
            "bots": _read_yaml(root / "bots.yaml"),
            "pilot": _read_yaml(root / "pilot.yaml"),
            "python_releases": _read_yaml(root / "python_releases.yaml"),
            "bq_approvals": _read_yaml(root / "bq_approvals.yaml"),
        }
    )
