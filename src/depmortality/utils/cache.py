"""On-disk cache of raw external responses, keyed by (source, query hash, snapshot date).

The date component is the data snapshot date from config (not today's date), so re-running
a pipeline never re-downloads unless asked to with ``refresh``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


class CacheMiss(RuntimeError):
    """Raised in cache-only mode when a response has not been downloaded yet."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def query_hash(query: Any) -> str:
    return hashlib.sha256(canonical_json(query).encode()).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class DiskCache:
    def __init__(
        self,
        root: Path,
        snapshot: dt.date,
        *,
        refresh: bool = False,
        cache_only: bool = False,
    ) -> None:
        if refresh and cache_only:
            raise ValueError("refresh and cache_only are mutually exclusive")
        self.root = root
        self.snapshot = snapshot
        self.refresh = refresh
        self.cache_only = cache_only
        self.hits = 0
        self.misses = 0

    def path_for(self, source: str, query: Any, suffix: str) -> Path:
        h = query_hash(query)
        return self.root / source / self.snapshot.isoformat() / h[:2] / f"{h}.{suffix}"

    def has(self, source: str, query: Any, suffix: str = "json") -> bool:
        return self.path_for(source, query, suffix).exists()

    def get_bytes(
        self, source: str, query: Any, fetch: Callable[[], bytes], *, suffix: str = "json"
    ) -> bytes:
        path = self.path_for(source, query, suffix)
        if path.exists() and not self.refresh:
            self.hits += 1
            return path.read_bytes()
        if self.cache_only:
            raise CacheMiss(f"{source}: not cached ({path.name}); query={canonical_json(query)}")
        self.misses += 1
        data = fetch()
        _atomic_write(path, data)
        sidecar = path.with_suffix(".query.json")
        _atomic_write(sidecar, canonical_json({"source": source, "query": query}).encode())
        return data

    def get_json(self, source: str, query: Any, fetch: Callable[[], Any]) -> Any:
        raw = self.get_bytes(source, query, lambda: json.dumps(fetch()).encode(), suffix="json")
        return json.loads(raw)
