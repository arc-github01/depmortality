"""Merge PyPI accounts, GitHub logins and commit emails into one person ID.

Merging is deliberately conservative (DECISIONS.md 2026-10-26):
- a GitHub noreply email (``[id+]login@users.noreply.github.com``) links to that login;
- an (email, login) pair reported by the GitHub API for a commit links the two;
- a PyPI account links to a GitHub login only on an exact case-insensitive match AND
  when that login is seen acting on the package's own repository;
- names are never used to merge, and generic emails are scoped to one repository so they
  cannot fuse unrelated people.

Anything written under data/processed/ uses ``hash_person`` (salted HMAC-SHA256) only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterable

NOREPLY = re.compile(
    r"^(?:\d+\+)?(?P<login>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)@users\.noreply\.github\.com$"
)
GENERIC_EMAIL = re.compile(
    r"^(?:$|[^@]*$|noreply@github\.com$|.*@example\.(?:com|org)$|.*@localhost(?:\..*)?$|"
    r"(?:root|admin|none|nobody|unknown|user|test|dev)@.*$|.*@\(none\)$|.*\.local$)"
)

Key = tuple[str, str]


class MissingSalt(RuntimeError):
    pass


def id_salt() -> str:
    salt = os.environ.get("DEPMORTALITY_ID_SALT", "")
    if len(salt) < 16:
        raise MissingSalt("DEPMORTALITY_ID_SALT must be set (at least 16 characters)")
    return salt


def hash_person(person: str, salt: str) -> str:
    return hmac.new(salt.encode(), person.encode(), hashlib.sha256).hexdigest()[:24]


def normalise_email(email: str) -> str:
    return email.strip().lower()


def login_key(login: str) -> Key:
    return ("gh", login.strip().lower())


def pypi_key(account: str) -> Key:
    return ("pypi", account.strip().lower())


def email_key(email: str, repo: str) -> Key:
    """Generic emails are scoped to the repo, so 'you@example.com' never spans projects."""
    e = normalise_email(email)
    if GENERIC_EMAIL.match(e):
        return ("email@" + repo.lower(), e)
    return ("email", e)


class IdentityResolver:
    """Union-find over identifier keys. The person ID is the smallest key in a component."""

    def __init__(self) -> None:
        self._parent: dict[Key, Key] = {}

    def add(self, key: Key) -> Key:
        self._parent.setdefault(key, key)
        return key

    def find(self, key: Key) -> Key:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: Key, b: Key) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def add_email(self, email: str, repo: str) -> Key:
        key = self.add(email_key(email, repo))
        match = NOREPLY.match(normalise_email(email))
        if match:
            self.union(key, login_key(match.group("login")))
        return key

    def link_email_login(self, email: str, login: str, repo: str) -> None:
        key = email_key(email, repo)
        if key[0] != "email":  # never merge a generic address into a person
            return
        self.union(key, login_key(login))

    def link_pypi_accounts(self, accounts: Iterable[str], repo_logins: Iterable[str]) -> None:
        seen = {login.strip().lower() for login in repo_logins}
        for account in accounts:
            if account.strip().lower() in seen:
                self.union(pypi_key(account), login_key(account))

    def person(self, key: Key) -> str:
        root = self.find(key)
        return f"{root[0]}:{root[1]}"
