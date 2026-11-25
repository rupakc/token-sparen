"""Content-type detection from tool/command hints and a sniff of the first 4 KB."""

from __future__ import annotations

import json
import os
import re
from collections import Counter

from token_sparen import config as _config
from token_sparen.compress.base import ContentType
from token_sparen.events import ToolCall

SNIFF_CHARS = 4096
JSON_MAX_CHARS = 4 * 1024 * 1024

_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)?)?")
_WRAPPERS = frozenset({"sudo", "time", "env", "command", "exec", "nice", "npx", "bunx"})
_RUNNERS = frozenset({"uv", "poetry", "pipenv"})  # `<runner> run <cmd>`
_TEST_CMDS = frozenset({"pytest", "py.test", "jest", "vitest"})
_COMPILERS = frozenset({"tsc", "rustc", "javac", "gcc", "g++", "cc", "clang", "clang++"})

_M = re.MULTILINE
_TEST_SNIFF = re.compile(
    r"^=+ test session starts"
    r"|^=+ [^=\n]*\b\d+ (?:passed|failed|errors?)\b"
    r"|^(?:PASS|FAIL)[ \t]\S"
    r"|^--- (?:FAIL|PASS): "
    r"|^test result: "
    r"|^running \d+ tests?$",
    _M,
)
_COMPILER_SNIFF = re.compile(
    r"\berror TS\d+"
    r"|\berror\[E\d+\]"
    r"|^[^\s:]+\.(?:c|cc|cpp|cxx|h|hpp|m|java|kt|scala|go|rs|ts|tsx|swift|cs):\d+(?::\d+)?:"
    r" (?:fatal )?(?:error|warning)\b"
    r"|COMPILATION ERROR",
    _M,
)
_BUILD_SNIFF = re.compile(
    r"^npm (?:ERR!|WARN)"
    r"|^added \d+ packages?"
    r"|^Collecting \S"
    r"|^Successfully installed "
    r"|^Requirement already satisfied"
    r"|^Step \d+/\d+ : "
    r"|^#\d+ \["
    r"|^BUILD (?:SUCCESSFUL|FAILED)"
    r"|^> Task :",
    _M,
)
_STACK_SNIFF = re.compile(
    r"Traceback \(most recent call last\)"
    r"|Exception in thread "
    r"|^[ \t]+at [\w$.<>/]+\([\w$]+\.(?:java|kt|scala|groovy):\d+\)"
    r"|^goroutine \d+ \[",
    _M,
)
# Node/JS: "XxxError: msg" then, within 3 lines, an "at ... :line:col" frame.
_NODE_STACK_SNIFF = re.compile(
    r"^[A-Za-z]*Error: [^\n]*(?:\n[^\n]*){0,3}?\n[ \t]+at [^\n]*:\d+:\d+\)?$",
    _M,
)
_LOG_LINE = re.compile(
    r"\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"  # ISO 8601
    r"|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}"  # syslog
)
_SEARCH_LINE = re.compile(r"[^\s:]+:\d+[:-]")
_TABLE_DELIMS = (",", "\t", "|")


def _strip_prefix(tokens: list[str]) -> list[str]:
    while tokens:
        head = tokens[0]
        if _ENV_ASSIGN.match(head) or head in _WRAPPERS:
            tokens = tokens[1:]
        elif head in _RUNNERS and len(tokens) > 1 and tokens[1] == "run":
            tokens = tokens[2:]
        else:
            break
    return tokens


def _classify(tokens: list[str]) -> ContentType | None:
    name = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    sub = args[0] if args else ""
    if _PYTHON.fullmatch(name) and len(args) >= 2 and args[0] == "-m":
        name, args = args[1], args[2:]
        sub = args[0] if args else ""
    if name in _TEST_CMDS:
        return "test_report"
    if name in {"npm", "yarn", "pnpm", "bun"}:
        if sub == "test" or (sub == "run" and args[1:2] == ["test"]):
            return "test_report"
        if sub in {"install", "ci", "i", "add"}:
            return "build_log"
        return None
    if name in {"go", "cargo"} and sub == "test":
        return "test_report"
    if name in _COMPILERS or (name == "cargo" and sub in {"build", "check"}):
        return "compiler"
    if name == "mvn" and sub == "compile":
        return "compiler"
    if name in {"pip", "pip3"} and sub == "install":
        return "build_log"
    if name in {"gradle", "gradlew"} or (name == "docker" and sub == "build"):
        return "build_log"
    if name in {"rg", "grep", "egrep", "fgrep"}:
        return "search"
    if name == "pplx":
        return "pplx"
    if name == "git" and sub == "diff":
        return "diff"
    return None


def command_hint(command: str) -> ContentType | None:
    """Classify a shell command by its first real token (after env assignments and `cd x &&`)."""
    for segment in re.split(r"&&|;", command):
        tokens = _strip_prefix(segment.split("|", 1)[0].split())
        if not tokens or tokens[0] == "cd":
            continue
        return _classify(tokens)
    return None


def _tool_hint(tool: ToolCall) -> ContentType | None:
    if tool.category == "search":
        return "search"
    cmd = tool.input.get("command")
    return command_hint(cmd) if isinstance(cmd, str) else None


def _ratio(lines: list[str], pat: re.Pattern[str]) -> float:
    return sum(1 for ln in lines if pat.match(ln)) / len(lines) if lines else 0.0


def _is_table(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    for delim in _TABLE_DELIMS:
        count, freq = Counter(ln.count(delim) for ln in lines).most_common(1)[0]
        if count >= 2 and freq >= 0.8 * len(lines):
            return True
    return False


def _is_json(text: str, head: str) -> bool:
    if head.lstrip()[:1] not in ("{", "[") or len(text) > JSON_MAX_CHARS:
        return False
    try:
        json.loads(text)
    except (ValueError, RecursionError):  # deep nesting overflows the decoder's stack
        return False
    return True


def sniff(text: str) -> ContentType:
    head = text[:SNIFF_CHARS]
    if len(text) > SNIFF_CHARS and "\n" in head:
        head = head.rsplit("\n", 1)[0]  # drop the truncated last line
    stripped = head.lstrip()
    if stripped.startswith(("diff --git", "@@")):
        return "diff"
    if _is_json(text, head):
        return "json"
    if _TEST_SNIFF.search(head):
        return "test_report"
    if _COMPILER_SNIFF.search(head):
        return "compiler"
    if _BUILD_SNIFF.search(head):
        return "build_log"
    if _STACK_SNIFF.search(head) or _NODE_STACK_SNIFF.search(head):
        return "stacktrace"
    lines = [ln for ln in head.split("\n") if ln.strip()]
    if _ratio(lines, _LOG_LINE) >= 0.5:
        return "log"
    if len(lines) >= 2 and _ratio(lines, _SEARCH_LINE) >= 0.6:
        return "search"
    if _is_table(lines):
        return "table"
    return "text"


def detect(text: str, tool: ToolCall | None = None) -> ContentType:
    """Classify tool output; a recognised tool/command hint wins over content sniffing.

    Never raises: any failure is logged and the output is treated as plain text.
    """
    try:
        return _detect(text, tool)
    except Exception as exc:  # noqa: BLE001 - detection must never break a hook
        _config.log_error(os.environ, "compress.detect", f"{type(exc).__name__}: {exc}")
        return "text"


def _detect(text: str, tool: ToolCall | None) -> ContentType:
    if tool is not None:
        hint = _tool_hint(tool)
        if hint is not None:
            return hint
    return sniff(text)
