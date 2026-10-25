from __future__ import annotations

import pytest

from depmortality.labels.identity import (
    IdentityResolver,
    MissingSalt,
    email_key,
    hash_person,
    id_salt,
    login_key,
    pypi_key,
)


def test_noreply_email_links_to_login() -> None:
    r = IdentityResolver()
    k1 = r.add_email("1324225+HugoVK@users.noreply.github.com", "python-pillow/pillow")
    k2 = r.add_email("hugovk@users.noreply.github.com", "python-pillow/pillow")
    assert r.person(k1) == r.person(k2) == r.person(login_key("hugovk"))


def test_api_mapping_merges_personal_email() -> None:
    r = IdentityResolver()
    e = r.add_email("jane.doe@uni.edu", "jdoe/tinyretry")
    r.link_email_login("Jane.Doe@uni.edu", "jdoe", "jdoe/tinyretry")
    assert r.person(e) == r.person(login_key("jdoe"))


def test_pypi_account_needs_exact_match_and_repo_presence() -> None:
    r = IdentityResolver()
    r.link_pypi_accounts(["jdoe", "someone-else"], repo_logins=["JDoe", "alice"])
    assert r.person(pypi_key("jdoe")) == r.person(login_key("jdoe"))
    assert r.person(pypi_key("someone-else")) != r.person(login_key("someone-else"))


def test_pypi_account_not_in_repo_is_not_merged() -> None:
    r = IdentityResolver()
    r.link_pypi_accounts(["alice"], repo_logins=["bob"])
    assert r.person(pypi_key("alice")) != r.person(login_key("alice"))


def test_generic_email_scoped_to_repo_and_never_merged() -> None:
    r = IdentityResolver()
    a = r.add_email("you@example.com", "org/one")
    b = r.add_email("you@example.com", "org/two")
    assert r.person(a) != r.person(b)
    r.link_email_login("you@example.com", "jdoe", "org/one")
    assert r.person(a) != r.person(login_key("jdoe"))
    assert email_key("root@localhost", "x/y")[0] == "email@x/y"


def test_names_are_never_used() -> None:
    r = IdentityResolver()
    a = r.add_email("j.smith@a.com", "x/y")
    b = r.add_email("john.smith@b.com", "x/y")
    assert r.person(a) != r.person(b)


def test_transitive_merge_is_deterministic() -> None:
    r1, r2 = IdentityResolver(), IdentityResolver()
    for r, order in ((r1, [0, 1]), (r2, [1, 0])):
        steps = [
            lambda r=r: r.link_email_login("a@x.org", "jdoe", "o/p"),
            lambda r=r: r.link_pypi_accounts(["jdoe"], ["jdoe"]),
        ]
        for i in order:
            steps[i]()
    assert (
        r1.person(pypi_key("jdoe"))
        == r2.person(pypi_key("jdoe"))
        == r1.person(email_key("a@x.org", "o/p"))
    )


def test_hash_is_salted_and_stable() -> None:
    assert hash_person("gh:jdoe", "s" * 16) == hash_person("gh:jdoe", "s" * 16)
    assert hash_person("gh:jdoe", "s" * 16) != hash_person("gh:jdoe", "t" * 16)
    assert "jdoe" not in hash_person("gh:jdoe", "s" * 16)


def test_salt_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPMORTALITY_ID_SALT", raising=False)
    with pytest.raises(MissingSalt):
        id_salt()
    monkeypatch.setenv("DEPMORTALITY_ID_SALT", "short")
    with pytest.raises(MissingSalt):
        id_salt()
