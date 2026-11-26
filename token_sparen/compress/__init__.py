"""Recoverable output compression: detection, compressors and their registry."""

from __future__ import annotations

from token_sparen.compress import relevance
from token_sparen.compress.base import (
    ENTRY_POINT_GROUP,
    ERROR_PATTERNS,
    CheckedCompressor,
    CompressContext,
    Compressor,
    CompressResult,
    ContentType,
    Level,
    Registry,
    content_lines,
    is_compressor,
    is_error_line,
    line_count,
    load_level,
    missing_error_lines,
    registry,
    result_problems,
    unwrap,
)
from token_sparen.compress.detect import detect
from token_sparen.compress.reports import (
    BuildLogCompressor,
    CompilerCompressor,
    TestReportCompressor,
)
from token_sparen.compress.search import LogCompressor, PplxCompressor, SearchCompressor
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
    SearchCompressor(),
    PplxCompressor(),
    LogCompressor(),
):
    registry.register(_builtin)

__all__ = [
    "BuildLogCompressor",
    "CompilerCompressor",
    "TestReportCompressor",
    "ENTRY_POINT_GROUP",
    "ERROR_PATTERNS",
    "CheckedCompressor",
    "CompressContext",
    "CompressResult",
    "Compressor",
    "DiffCompressor",
    "JsonCompressor",
    "LogCompressor",
    "PplxCompressor",
    "SearchCompressor",
    "ContentType",
    "Level",
    "Registry",
    "StackTraceCompressor",
    "TextCompressor",
    "detect",
    "content_lines",
    "is_compressor",
    "is_error_line",
    "line_count",
    "load_level",
    "missing_error_lines",
    "registry",
    "relevance",
    "result_problems",
    "unwrap",
]
