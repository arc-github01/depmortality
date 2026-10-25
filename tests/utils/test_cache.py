from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from depmortality.utils.cache import CacheMiss, DiskCache

SNAP = dt.date(2026, 10, 26)


def counter() -> tuple[list[int], object]:
    calls: list[int] = []

    def fetch() -> bytes:
        calls.append(1)
        return f"payload-{len(calls)}".encode()

    return calls, fetch


def test_second_read_hits_cache(tmp_path: Path) -> None:
    calls, fetch = counter()
    c = DiskCache(tmp_path, SNAP)
    assert c.get_bytes("pypi", {"name": "six"}, fetch) == b"payload-1"  # type: ignore[arg-type]
    assert c.get_bytes("pypi", {"name": "six"}, fetch) == b"payload-1"  # type: ignore[arg-type]
    assert len(calls) == 1 and c.hits == 1


def test_key_includes_source_query_and_snapshot(tmp_path: Path) -> None:
    c1 = DiskCache(tmp_path, SNAP)
    c2 = DiskCache(tmp_path, SNAP + dt.timedelta(days=1))
    p = c1.path_for("pypi", {"name": "six"}, "json")
    assert p != c1.path_for("osv", {"name": "six"}, "json")
    assert p != c1.path_for("pypi", {"name": "attrs"}, "json")
    assert p != c2.path_for("pypi", {"name": "six"}, "json")
    assert c1.path_for("pypi", {"a": 1, "b": 2}, "json") == c1.path_for(
        "pypi", {"b": 2, "a": 1}, "json"
    )


def test_refresh_refetches(tmp_path: Path) -> None:
    calls, fetch = counter()
    DiskCache(tmp_path, SNAP).get_bytes("pypi", "q", fetch)  # type: ignore[arg-type]
    out = DiskCache(tmp_path, SNAP, refresh=True).get_bytes("pypi", "q", fetch)  # type: ignore[arg-type]
    assert out == b"payload-2" and len(calls) == 2


def test_cache_only_raises_on_miss(tmp_path: Path) -> None:
    _, fetch = counter()
    with pytest.raises(CacheMiss):
        DiskCache(tmp_path, SNAP, cache_only=True).get_bytes("pypi", "q", fetch)  # type: ignore[arg-type]


def test_json_round_trip(tmp_path: Path) -> None:
    c = DiskCache(tmp_path, SNAP)
    assert c.get_json("osv", {"q": 1}, lambda: {"vulns": [1, 2]}) == {"vulns": [1, 2]}
    assert DiskCache(tmp_path, SNAP, cache_only=True).get_json("osv", {"q": 1}, lambda: None) == {
        "vulns": [1, 2]
    }
