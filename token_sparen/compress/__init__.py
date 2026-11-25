"""Recoverable output compression: detection, compressors and their registry."""

from __future__ import annotations

from token_sparen.compress import relevance
from token_sparen.compress.base import (
    ENTRY_POINT_GROUP,
    ERROR_PATTERNS,
    CompressContext,
    Compressor,
    CompressResult,
    ContentType,
    Level,
    Registry,
    is_error_line,
    load_level,
    registry,
)
from token_sparen.compress.detect import detect
from token_sparen.compress.text import TextCompressor

__all__ = [
    "ENTRY_POINT_GROUP",
    "ERROR_PATTERNS",
    "CompressContext",
    "CompressResult",
    "Compressor",
    "ContentType",
    "Level",
    "Registry",
    "TextCompressor",
    "detect",
    "is_error_line",
    "load_level",
    "registry",
    "relevance",
]
