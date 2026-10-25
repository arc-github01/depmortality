from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from depmortality.config import Settings
from depmortality.utils.logsetup import REDACTED, RedactingFilter, redact
from depmortality.utils.paths import REPO_ROOT
from depmortality.utils.seeding import derive_seed, rng

FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def test_env_token_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "my-secret-token-value")
    assert "my-secret-token-value" not in redact("auth with my-secret-token-value failed")


def test_token_patterns_redacted() -> None:
    assert FAKE_TOKEN not in redact(f"token={FAKE_TOKEN}")
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert REDACTED in redact("github_pat_" + "x" * 30)


def test_filter_redacts_formatted_args(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="test.redaction"):
        logger.info("calling with %s", FAKE_TOKEN)
    assert FAKE_TOKEN not in caplog.text
    assert REDACTED in caplog.text


def test_split_dates_only_in_config() -> None:
    """Rule 3: the split dates are not hard-coded anywhere in src/."""
    pattern = re.compile(r"2021-12-31|2022-12-31|2023-01-01|2021,\s*12,\s*31|2023,\s*1,\s*1\b")
    offenders = [
        p
        for p in (REPO_ROOT / "src").rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_config_loads_and_is_consistent(settings: Settings) -> None:
    assert settings.splits.train_end < settings.splits.calibration_start
    assert settings.thresholds.states.quiet_window_months == 12
    assert settings.thresholds.bigquery.max_gb_per_query == 50
    assert "3.12" in settings.python_releases.releases


def test_seeds_are_deterministic() -> None:
    assert derive_seed(1, "a") == derive_seed(1, "a") != derive_seed(1, "b")
    assert rng(7, "x").integers(0, 10**9) == rng(7, "x").integers(0, 10**9)


def test_env_file_is_gitignored() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines and "data/" in lines
    assert not (REPO_ROOT / ".env").exists() or ".env" in lines
    assert isinstance(REPO_ROOT, Path)
