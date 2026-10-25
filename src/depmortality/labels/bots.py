"""Bot detection. Bot activity is removed before any activity count or state (rule 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from depmortality.config import Bots

_SEPARATORS = re.compile(r"[-_.\s\[\]()]+")


@dataclass(frozen=True)
class BotFilter:
    logins: frozenset[str]
    names: frozenset[str]
    email_fragments: tuple[str, ...]
    heuristic_tokens: frozenset[str]

    @classmethod
    def from_config(cls, cfg: Bots) -> BotFilter:
        return cls(
            logins=frozenset(s.lower() for s in cfg.logins),
            names=frozenset(s.lower() for s in cfg.names),
            email_fragments=tuple(s.lower() for s in cfg.email_fragments),
            heuristic_tokens=frozenset(s.lower() for s in cfg.heuristic_tokens),
        )

    def _heuristic(self, text: str) -> bool:
        tokens = [t for t in _SEPARATORS.split(text.lower()) if t]
        # A single token ("bot", "robot", "abbott") is never enough: only a
        # separator-delimited bot token alongside another token, e.g. "pyup-bot".
        return len(tokens) > 1 and any(t in self.heuristic_tokens for t in tokens)

    def is_bot(
        self,
        login: str | None = None,
        name: str | None = None,
        email: str | None = None,
    ) -> bool:
        if login:
            low = login.strip().lower()
            if low.endswith("[bot]") or low in self.logins or self._heuristic(low):
                return True
        if name:
            low = name.strip().lower()
            # No token heuristic on display names: people have surnames like "Bot".
            if low.endswith("[bot]") or low in self.names:
                return True
        if email:
            low = email.strip().lower()
            if any(fragment in low for fragment in self.email_fragments):
                return True
        return False
