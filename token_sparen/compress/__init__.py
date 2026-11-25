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
from token_sparen.compress.reports import (
    BuildLogCompressor,
    CompilerCompressor,
    TestReportCompressor,
)
from token_sparen.compress.structured import DiffCompressor, JsonCompressor, StackTraceCompressor
from token_sparen.compress.text import TextCompressor

for _builtin in (
    TextCompressor(),
    JsonCompressor(),
    DiffCompressor(),
    StackTraceCompressor(),
    TestReportCompressor(),
    CompilerCompressor(),
    BuildLogCompressor(),
):
    registry.register(_builtin)

__all__ = [
    "BuildLogCompressor",
    "CompilerCompressor",
    "TestReportCompressor",
    "ENTRY_POINT_GROUP",
    "ERROR_PATTERNS",
    "CompressContext",
    "CompressResult",
    "Compressor",
    "DiffCompressor",
    "JsonCompressor",
    "ContentType",
    "Level",
    "Registry",
    "StackTraceCompressor",
    "TextCompressor",
    "detect",
    "is_error_line",
    "load_level",
    "registry",
    "relevance",
]
