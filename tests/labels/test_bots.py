from __future__ import annotations

import pytest

from depmortality.labels.bots import BotFilter


@pytest.mark.parametrize(
    ("login", "name", "email"),
    [
        ("dependabot[bot]", None, None),
        ("renovate[bot]", None, None),
        ("pre-commit-ci[bot]", None, None),
        ("github-actions[bot]", None, None),
        ("dependabot-preview[bot]", None, None),
        ("Dependabot", None, None),  # listed login, any case
        ("pyup-bot", None, None),  # heuristic: separator-delimited "bot" token
        ("sympy-bot", None, None),
        ("codecov-io", None, None),
        ("web-flow", None, None),
        (None, "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
        (None, "github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com"),
        (None, "pre-commit-ci[bot]", "66853113+pre-commit-ci[bot]@users.noreply.github.com"),
        (None, "Renovate Bot", "bot@renovateapp.com"),
        (None, "pyup.io bot", "github-bot@pyup.io"),
        (None, "Weblate (bot)", "noreply@weblate.org"),
    ],
)
def test_bots_detected(
    bots: BotFilter, login: str | None, name: str | None, email: str | None
) -> None:
    assert bots.is_bot(login=login, name=name, email=email)


@pytest.mark.parametrize(
    ("login", "name", "email"),
    [
        ("abbott", None, None),
        ("botanist", None, None),
        ("robotframework", None, None),
        ("bot", None, None),  # a lone token is not enough
        ("cbotb", None, None),
        ("talbot-j", None, None),
        ("jdoe", "Jane Doe", "jane@example.org"),
        (None, "Ana Bot", "ana.bot@uni.edu"),  # surname "Bot": no name heuristic
        ("hugovk", "Hugo van Kemenade", "1324225+hugovk@users.noreply.github.com"),
        ("botbot-dev", None, None),  # "botbot" is not the token "bot"
    ],
)
def test_humans_not_flagged(
    bots: BotFilter, login: str | None, name: str | None, email: str | None
) -> None:
    assert not bots.is_bot(login=login, name=name, email=email)


def test_bot_list_comes_from_config(settings: object) -> None:
    from depmortality.config import Settings

    assert isinstance(settings, Settings)
    assert "dependabot" in [s.lower() for s in settings.bots.logins]
