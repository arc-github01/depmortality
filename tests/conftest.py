from __future__ import annotations

import pytest

from depmortality.config import Settings, load_settings
from depmortality.labels.bots import BotFilter


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def bots(settings: Settings) -> BotFilter:
    return BotFilter.from_config(settings.bots)
