"""Cheap token estimation and relative cost weighting."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from token_sparen.config import Config, load_config

RATIOS: dict[str, float] = {"json": 3.2, "code": 3.5, "text": 4.0, "log": 3.6}


def estimate_tokens(text: str, content_type: str = "text") -> int:
    """Estimate tokens as chars / ratio, rounded up. Unknown types use the text ratio."""
    ratio = RATIOS.get(content_type, RATIOS["text"])
    # Integer arithmetic avoids float artefacts such as 320 / 3.2 > 100.
    return math.ceil(len(text) * 10 / round(ratio * 10))


@dataclass(frozen=True)
class Prices:
    """Relative list-price ratios per token class (input = 1.0)."""

    input: float = 1.0
    cache_write: float = 1.25
    cache_read: float = 0.1
    output: float = 5.0

    @classmethod
    def load(cls, config: Config | None = None) -> Prices:
        cfg = config or load_config(Path.cwd(), os.environ)
        d = cls()
        return cls(
            input=float(cfg.get("pricing.input", d.input)),
            cache_write=float(cfg.get("pricing.cache_write", d.cache_write)),
            cache_read=float(cfg.get("pricing.cache_read", d.cache_read)),
            output=float(cfg.get("pricing.output", d.output)),
        )


@dataclass(frozen=True)
class Usage:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    def cost_weighted(self, prices: Prices) -> float:
        return (
            self.input * prices.input
            + self.cache_write * prices.cache_write
            + self.cache_read * prices.cache_read
            + self.output * prices.output
        )
