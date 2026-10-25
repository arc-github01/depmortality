"""Standard on-disk locations. Everything under data/ is git-ignored."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def interim(self) -> Path:
        """Pulled and parsed tables. May contain emails; never released."""
        return self.root / "interim"

    @property
    def processed(self) -> Path:
        """Analysis tables. Identities appear only as salted hashes."""
        return self.root / "processed"

    @property
    def bq_ledger(self) -> Path:
        return self.root / "bq_ledger.jsonl"

    @property
    def api_ledger(self) -> Path:
        return self.root / "api_ledger.jsonl"

    @property
    def git_mirrors(self) -> Path:
        return self.root / "git"

    def ensure(self) -> DataPaths:
        for p in (self.raw, self.interim, self.processed, self.git_mirrors):
            p.mkdir(parents=True, exist_ok=True)
        return self


def default_paths() -> DataPaths:
    return DataPaths(REPO_ROOT / "data")


def reports_dir() -> Path:
    return REPO_ROOT / "reports"
