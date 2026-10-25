"""PyPI: release history (JSON API), PEP 792 status (Simple API), maintainer roles (XML-RPC).

Per-release classifiers come from BigQuery (``depsdev.sql_release_classifiers``) instead of
one JSON call per version. Everything here is cached via DiskCache.
"""

from __future__ import annotations

import datetime as dt
import json
import xmlrpc.client
from dataclasses import dataclass, field
from typing import Any

from depmortality.labels.triggers import Release, WheelTag, parse_wheel_filename
from depmortality.utils.cache import DiskCache
from depmortality.utils.http import HttpClient

JSON_URL = "https://pypi.org/pypi/{name}/json"
SIMPLE_URL = "https://pypi.org/simple/{name}/"
XMLRPC_URL = "https://pypi.org/pypi"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"


class NotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    releases: tuple[Release, ...]
    project_urls: dict[str, str] = field(default_factory=dict)
    home_page: str | None = None
    latest_classifiers: tuple[str, ...] = ()
    requires_python: str | None = None
    author_email: str | None = None
    maintainer_email: str | None = None


def _day(text: str) -> dt.date:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def parse_project_json(name: str, payload: dict[str, Any]) -> ProjectInfo:
    info = payload.get("info") or {}
    releases: list[Release] = []
    for version, files in sorted((payload.get("releases") or {}).items()):
        if not files:
            continue  # a version with no files was never uploaded
        days = [
            _day(f.get("upload_time_iso_8601") or f["upload_time"])
            for f in files
            if f.get("upload_time_iso_8601") or f.get("upload_time")
        ]
        if not days:
            continue
        wheels: list[WheelTag] = []
        for f in files:
            tag = parse_wheel_filename(f.get("filename", ""))
            if tag is not None and tag not in wheels:
                wheels.append(tag)
        requires = next((f.get("requires_python") for f in files if f.get("requires_python")), None)
        releases.append(
            Release(
                version=version,
                upload_day=min(days),
                yanked=all(bool(f.get("yanked")) for f in files),
                requires_python=requires,
                wheels=tuple(wheels),
            )
        )
    releases.sort(key=lambda r: (r.upload_day, r.version))
    return ProjectInfo(
        name=name,
        releases=tuple(releases),
        project_urls=dict(info.get("project_urls") or {}),
        home_page=info.get("home_page") or None,
        latest_classifiers=tuple(info.get("classifiers") or ()),
        requires_python=info.get("requires_python") or None,
        author_email=info.get("author_email") or None,
        maintainer_email=info.get("maintainer_email") or None,
    )


def parse_project_status(payload: dict[str, Any]) -> str | None:
    """PEP 792 ``project-status`` from the Simple API JSON, or None if not exposed."""
    status = payload.get("project-status")
    if isinstance(status, dict):
        value = status.get("status")
        return str(value).lower() if value else None
    if isinstance(status, str):
        return status.lower()
    return None


def parse_roles(xml: bytes) -> list[tuple[str, str]]:
    """XML-RPC package_roles -> [(role, account)]."""
    params, _ = xmlrpc.client.loads(xml.decode())
    result: list[list[str]] = params[0]  # type: ignore[assignment]
    return [(str(pair[0]), str(pair[1])) for pair in result]


class PyPIClient:
    def __init__(self, http: HttpClient, cache: DiskCache) -> None:
        self.http = http
        self.cache = cache

    def project(self, name: str) -> ProjectInfo:
        def fetch() -> bytes:
            r = self.http.request("GET", JSON_URL.format(name=name))
            if r.status_code == 404:
                return b'{"__not_found__": true}'
            r.raise_for_status()
            return r.content

        payload = json.loads(self.cache.get_bytes("pypi_json", {"name": name}, fetch))
        if payload.get("__not_found__"):
            raise NotFound(name)
        return parse_project_json(name, payload)

    def status(self, name: str) -> str | None:
        def fetch() -> bytes:
            r = self.http.request(
                "GET", SIMPLE_URL.format(name=name), headers={"Accept": SIMPLE_JSON}
            )
            if r.status_code == 404:
                return b"{}"
            r.raise_for_status()
            return r.content

        return parse_project_status(
            json.loads(self.cache.get_bytes("pypi_simple", {"name": name}, fetch))
        )

    def roles(self, name: str) -> list[tuple[str, str]]:
        def fetch() -> bytes:
            body = xmlrpc.client.dumps((name,), "package_roles")
            r = self.http.request(
                "POST", XMLRPC_URL, content=body.encode(), headers={"Content-Type": "text/xml"}
            )
            r.raise_for_status()
            return r.content

        return parse_roles(self.cache.get_bytes("pypi_roles", {"name": name}, fetch, suffix="xml"))
