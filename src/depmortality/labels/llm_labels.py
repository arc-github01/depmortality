"""LLM labelling of READMEs and maintainer comments (trigger (d) and "finished" validation).

Out of scope for Phase 0 (DECISIONS.md 2026-10-26). Phase 1 implements this with a
versioned prompt file and a 200-item hand-labelled audit.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

StatementLabel = Literal["unmaintained", "finished", "neither"]


def classify_statement(readme: str | None, maintainer_comments: Sequence[str]) -> StatementLabel:
    raise NotImplementedError("LLM statement extraction is Phase 1 scope")
