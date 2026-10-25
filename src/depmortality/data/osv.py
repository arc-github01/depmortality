"""OSV advisories per PyPI package (https://api.osv.dev)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

from depmortality.data.depsdev import normalise_name
from depmortality.labels.triggers import Advisory
from depmortality.utils.cache import DiskCache
from depmortality.utils.http import HttpClient

QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
VULN_URL = "https://api.osv.dev/v1/vulns/{id}"
BATCH = 500


def _day(text: str | None) -> dt.date | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def parse_vuln(payload: dict[str, Any], package: str) -> list[Advisory]:
    """One Advisory per ``affected`` entry for this PyPI package (ECOSYSTEM ranges only)."""
    published = _day(payload.get("published"))
    if published is None:
        return []
    out = []
    target = normalise_name(package)
    for aff in payload.get("affected") or []:
        pkg = aff.get("package") or {}
        if pkg.get("ecosystem") != "PyPI" or normalise_name(pkg.get("name", "")) != target:
            continue
        fixed: list[str] = []
        last: list[str] = []
        for rng in aff.get("ranges") or []:
            if rng.get("type") != "ECOSYSTEM":
                continue
            for event in rng.get("events") or []:
                if "fixed" in event:
                    fixed.append(str(event["fixed"]))
                if "last_affected" in event:
                    last.append(str(event["last_affected"]))
        out.append(
            Advisory(
                vuln_id=str(payload["id"]),
                published=published,
                withdrawn=_day(payload.get("withdrawn")),
                fixed=tuple(fixed),
                last_affected=tuple(last),
                affected_versions=tuple(str(v) for v in aff.get("versions") or ()),
                aliases=tuple(str(a) for a in payload.get("aliases") or ()),
            )
        )
    return out


class OSVClient:
    def __init__(self, http: HttpClient, cache: DiskCache) -> None:
        self.http = http
        self.cache = cache

    def vuln_ids(self, packages: Sequence[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        names = sorted(set(packages))
        for start in range(0, len(names), BATCH):
            chunk = names[start : start + BATCH]
            pending = {n: None for n in chunk}  # name -> page token
            while pending:
                queries = [
                    {
                        "package": {"name": n, "ecosystem": "PyPI"},
                        **({"page_token": t} if t else {}),
                    }
                    for n, t in pending.items()
                ]

                def fetch(queries: list[dict[str, Any]] = queries) -> bytes:
                    r = self.http.request("POST", QUERYBATCH_URL, json={"queries": queries})
                    r.raise_for_status()
                    return r.content

                data = json.loads(self.cache.get_bytes("osv_batch", {"queries": queries}, fetch))
                nxt: dict[str, Any] = {}
                for name, result in zip(pending, data.get("results") or [], strict=True):
                    out.setdefault(name, []).extend(v["id"] for v in result.get("vulns") or [])
                    if result.get("next_page_token"):
                        nxt[name] = result["next_page_token"]
                pending = nxt
        return out

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        def fetch() -> bytes:
            r = self.http.request("GET", VULN_URL.format(id=vuln_id))
            r.raise_for_status()
            return r.content

        payload: dict[str, Any] = json.loads(
            self.cache.get_bytes("osv_vuln", {"id": vuln_id}, fetch)
        )
        return payload

    def advisories(self, packages: Sequence[str]) -> dict[str, list[Advisory]]:
        ids = self.vuln_ids(packages)
        return {
            name: [adv for vid in sorted(set(vids)) for adv in parse_vuln(self.vuln(vid), name)]
            for name, vids in ids.items()
        }
