"""GitHub GraphQL: repo metadata (archived + date, ids, default branch, owner type), commit
author logins for identity merging, and README text.

Repository and owner names are validated before being placed in a query string.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from depmortality.utils.cache import DiskCache
from depmortality.utils.http import HttpClient, github_token

GRAPHQL_URL = "https://api.github.com/graphql"
README_URL = "https://api.github.com/repos/{repo}/readme"
REPOS_PER_QUERY = 50
COMMITS_PER_QUERY = 100
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

REPO_FIELDS = """
  databaseId nameWithOwner isArchived archivedAt createdAt pushedAt isFork isMirror
  defaultBranchRef { name }
  owner { __typename login }
"""


@dataclass(frozen=True)
class RepoMeta:
    repo: str  # as requested, lower-case owner/name
    found: bool
    database_id: int | None = None
    name_with_owner: str | None = None
    is_archived: bool = False
    archived_at: dt.date | None = None
    created_at: dt.date | None = None
    pushed_at: dt.date | None = None
    is_fork: bool = False
    is_mirror: bool = False
    default_branch: str | None = None
    owner_type: str | None = None  # "User" | "Organization"
    owner_login: str | None = None


def _day(text: str | None) -> dt.date | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _split(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    if not (_SAFE.match(owner) and _SAFE.match(name)):
        raise ValueError(f"unsafe repository name: {repo!r}")
    return owner, name


def repo_query(repos: Sequence[str]) -> str:
    parts = []
    for i, repo in enumerate(repos):
        owner, name = _split(repo)
        parts.append(f'r{i}: repository(owner: "{owner}", name: "{name}") {{{REPO_FIELDS}}}')
    return "query {\n" + "\n".join(parts) + "\n rateLimit { cost remaining resetAt }\n}"


def parse_repo_response(repos: Sequence[str], payload: dict[str, Any]) -> list[RepoMeta]:
    data = payload.get("data") or {}
    out = []
    for i, repo in enumerate(repos):
        node = data.get(f"r{i}")
        if not node:
            out.append(RepoMeta(repo=repo, found=False))
            continue
        owner = node.get("owner") or {}
        branch = node.get("defaultBranchRef") or {}
        out.append(
            RepoMeta(
                repo=repo,
                found=True,
                database_id=node.get("databaseId"),
                name_with_owner=node.get("nameWithOwner"),
                is_archived=bool(node.get("isArchived")),
                archived_at=_day(node.get("archivedAt")),
                created_at=_day(node.get("createdAt")),
                pushed_at=_day(node.get("pushedAt")),
                is_fork=bool(node.get("isFork")),
                is_mirror=bool(node.get("isMirror")),
                default_branch=branch.get("name"),
                owner_type=owner.get("__typename"),
                owner_login=owner.get("login"),
            )
        )
    return out


def commit_login_query(repo: str, shas: Sequence[str]) -> str:
    owner, name = _split(repo)
    parts = []
    for i, sha in enumerate(shas):
        if not _SHA.match(sha):
            raise ValueError(f"bad sha {sha!r}")
        fields = "... on Commit { author { email user { login } } }"
        parts.append(f'c{i}: object(oid: "{sha}") {{ {fields} }}')
    body = "\n".join(parts)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{\n{body}\n}} }}'


def parse_commit_logins(shas: Sequence[str], payload: dict[str, Any]) -> list[tuple[str, str]]:
    """-> [(email, login)] for commits GitHub attributes to a user."""
    repo = (payload.get("data") or {}).get("repository") or {}
    pairs = []
    for i, _ in enumerate(shas):
        node = repo.get(f"c{i}") or {}
        author = node.get("author") or {}
        user = author.get("user") or {}
        if author.get("email") and user.get("login"):
            pairs.append((str(author["email"]), str(user["login"])))
    return pairs


class GitHubClient:
    def __init__(self, http: HttpClient, cache: DiskCache) -> None:
        self.http = http
        self.cache = cache

    def _graphql(self, query: str, source: str) -> dict[str, Any]:
        def fetch() -> bytes:
            r = self.http.request(
                "POST",
                GRAPHQL_URL,
                json={"query": query},
                headers={"Authorization": f"bearer {github_token()}"},
            )
            r.raise_for_status()
            return r.content

        payload: dict[str, Any] = json.loads(self.cache.get_bytes(source, {"query": query}, fetch))
        return payload

    def repos(self, repos: Sequence[str]) -> list[RepoMeta]:
        out: list[RepoMeta] = []
        ordered = sorted(set(repos))
        for start in range(0, len(ordered), REPOS_PER_QUERY):
            chunk = ordered[start : start + REPOS_PER_QUERY]
            out.extend(parse_repo_response(chunk, self._graphql(repo_query(chunk), "github_repos")))
        return out

    def commit_logins(self, repo: str, shas: Sequence[str]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for start in range(0, len(shas), COMMITS_PER_QUERY):
            chunk = list(shas[start : start + COMMITS_PER_QUERY])
            pairs.extend(
                parse_commit_logins(
                    chunk, self._graphql(commit_login_query(repo, chunk), "github_commits")
                )
            )
        return pairs

    def readme(self, repo: str) -> str | None:
        _split(repo)

        def fetch() -> bytes:
            r = self.http.request(
                "GET",
                README_URL.format(repo=repo),
                headers={
                    "Authorization": f"bearer {github_token()}",
                    "Accept": "application/vnd.github.raw+json",
                },
            )
            if r.status_code == 404:
                return b""
            r.raise_for_status()
            return r.content

        text = self.cache.get_bytes("github_readme", {"repo": repo}, fetch, suffix="md")
        return text.decode("utf-8", errors="replace") or None
