"""Deterministic seeding. Every random operation takes an explicit seed from config."""

from __future__ import annotations

import hashlib

import numpy as np


def derive_seed(base: int, *keys: str | int) -> int:
    """A stable child seed for a named sub-task, independent of call order."""
    material = ":".join([str(base), *map(str, keys)]).encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def rng(seed: int, *keys: str | int) -> np.random.Generator:
    return np.random.default_rng(derive_seed(seed, *keys) if keys else seed)
