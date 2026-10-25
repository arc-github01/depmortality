"""Logging setup with secret redaction. Tokens must never reach a log line."""

from __future__ import annotations

import logging
import os
import re

SECRET_ENV_VARS = ("GITHUB_TOKEN", "DEPMORTALITY_ID_SALT", "GOOGLE_APPLICATION_CREDENTIALS")
REDACTED = "[REDACTED]"

# GitHub token formats, bearer headers, and Google private-key material.
_TOKEN_PATTERNS = [
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|token)\s+\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            text = text.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    for existing in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in existing.filters):
            existing.addFilter(RedactingFilter())
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
