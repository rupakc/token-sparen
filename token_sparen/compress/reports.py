"""Report compressors: test runners, compilers and build logs.

Top rule: a failing test id, its first assertion/error block and every line matched by
`ERROR_PATTERNS` always survive. All three are linear-time, never raise (the text compressor
takes over on any failure) and hand unrecognised output to the generic `TextCompressor`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from token_sparen.compress._shared import (
    arm,
    choice,
    guarded,
    setting,
    split_lines,
    unchanged,
)
from token_sparen.compress.base import (
    CompressContext,
    CompressResult,
    ContentType,
    Level,
    error_line_indices,
    is_error_line,
)
from token_sparen.compress.text import TextCompressor, elided_line

_TEXT = TextCompressor()
_M = re.MULTILINE


def _finish(
    text: str, out: list[str], trailer: str, kept: int, total: int, force: bool = False
) -> CompressResult:
    result = "\n".join(out) + trailer
    if not force and len(result) >= len(text):
        return unchanged(text, total)
    return CompressResult(text=result, kept_lines=kept, total_lines=total, lossless=False)


def _select(lines: list[str], idxs: list[int], head: int, pinned: set[int], out: list[str]) -> int:
    """Append the first `head` of `idxs` plus `pinned` ones, with gap markers; return kept."""
    keep = set(idxs[:head]) | {i for i in idxs if i in pinned}
    kept = 0
    gap = 0
    for i in idxs:
        if i in keep:
            if gap:
                out.append(elided_line(gap))
                gap = 0
            out.append(lines[i])
            kept += 1
        else:
            gap += 1
    if gap:
        out.append(elided_line(gap))
    return kept


# ------------------------------------------------------------------ test reports

_PY_SECTION = re.compile(r"^=+ (.+?) =+$")
_PY_HEADER = re.compile(r"^_{2,} (.+?) _{2,}$")
_PY_SUMMARY = re.compile(
    r"^(?:=+ )?(?:\d+ (?:failed|passed|skipped|errors?|warnings?|xfailed|xpassed|deselected)"
    r"[ ,]*)+.*\bin \d"
)
_JEST_SUMMARY = re.compile(r"^(?:Test Suites|Tests|Snapshots|Time|Ran all test suites)\b")
_JEST_END = re.compile(
    r"^(?:PASS|FAIL) |^\s*● |^(?:Summary of all failing tests|Test Suites:|Tests:|Snapshots:|Time:)"
)
_GO_SUMMARY = re.compile(r"^(?:ok|FAIL|\?)\s*\t|^(?:FAIL|PASS)$")
_GO_HEADER = re.compile(r"^\s*--- FAIL: ")
_GO_PASS = re.compile(r"^\s*--- PASS: ")
_GO_PANIC_END = re.compile(r"^(?:FAIL|exit status|---|===|ok\s)")
_CARGO_TEST = re.compile(r"^test (.+) \.\.\. (\w+)")
_CARGO_BLOCK = re.compile(r"^---- .+ ----$")
_PASSED = re.compile(r"(\d+) passed")


@dataclass
class _Parsed:
    summary: list[int] = field(default_factory=list)
    keep: list[int] = field(default_factory=list)
    blocks: list[tuple[int, list[int]]] = field(default_factory=list)
    passed: int = 0
    e_prefix: str | None = None


def _trim(body: list[int], lines: list[str]) -> list[int]:
    while body and not lines[body[-1]].strip():
        body.pop()
    return body


def _sniff(lines: list[str]) -> str | None:
    for line in lines:
        c = line[:1]
        if c == "=" and (_PY_SECTION.match(line) or _PY_SUMMARY.match(line)):
            return "pytest"
        if c.isdigit() and _PY_SUMMARY.match(line):
            return "pytest"
        if line.startswith(("PASS ", "FAIL ")) or _JEST_SUMMARY.match(line):
            return "jest"
        if line.lstrip().startswith("--- ") or _GO_SUMMARY.match(line):
            return "go"
        if line.startswith(("test result:", "test ")) and (
            line.startswith("test result:") or _CARGO_TEST.match(line)
        ):
            return "cargo"
    return None


def _parse_pytest(lines: list[str]) -> _Parsed:
    p = _Parsed(e_prefix="E ")
    mode = ""
    cur: tuple[int, list[int]] | None = None
    last_summary = -1
    for i, line in enumerate(lines):
        if line.startswith("="):
            m = _PY_SECTION.match(line)
            if m:
                cur = None
                name = m.group(1)
                if _PY_SUMMARY.match(name):
                    last_summary = i
                    mode = ""
                elif name in ("FAILURES", "ERRORS"):
                    mode = "blocks"
                elif name.startswith("short test summary"):
                    mode = "short"
                else:
                    mode = ""
                continue
        elif line[:1].isdigit() and _PY_SUMMARY.match(line):
            last_summary = i
            continue
        if mode == "blocks":
            h = _PY_HEADER.match(line)
            if h:
                cur = (i, [])
                p.blocks.append(cur)
            elif cur is not None:
                cur[1].append(i)
        elif mode == "short" and line.startswith(("FAILED ", "ERROR ")):
            p.keep.append(i)
    if last_summary >= 0:
        p.summary.append(last_summary)
        m2 = _PASSED.search(lines[last_summary])
        p.passed = int(m2.group(1)) if m2 else 0
    p.blocks = [(h, _trim(b, lines)) for h, b in p.blocks]
    return p


def _parse_jest(lines: list[str]) -> _Parsed:
    p = _Parsed()
    cur: tuple[int, list[int]] | None = None
    ticks = 0
    for i, line in enumerate(lines):
        if _JEST_END.match(line):
            cur = None
            if line.startswith("FAIL "):
                p.keep.append(i)
                continue
            if line.startswith("PASS "):
                continue
            if line.lstrip().startswith("● "):
                if line.lstrip().startswith("● Console"):
                    continue
                cur = (i, [])
                p.blocks.append(cur)
                continue
        if _JEST_SUMMARY.match(line):
            p.summary.append(i)
            m = _PASSED.search(line) if line.startswith("Tests:") else None
            if m:
                p.passed = int(m.group(1))
        elif cur is not None:
            cur[1].append(i)
        elif line.lstrip().startswith(("✓", "✔")):
            ticks += 1
    if not p.passed:
        p.passed = ticks
    p.blocks = [(h, _trim(b, lines)) for h, b in p.blocks]
    return p


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _parse_go(lines: list[str]) -> _Parsed:
    p = _Parsed()
    pending: list[int] = []
    cur: tuple[int, list[int]] | None = None
    base = 0
    panic = False
    for i, line in enumerate(lines):
        if panic:
            if _GO_PANIC_END.match(line):
                panic = False
            else:
                assert cur is not None
                cur[1].append(i)
                continue
        if _GO_HEADER.match(line):
            cur = (i, pending)
            base = _indent(line)
            pending = []
            p.blocks.append(cur)
            continue
        if _GO_PASS.match(line):
            p.passed += 1
            cur, pending = None, []
            continue
        if line.startswith("panic: "):
            cur = (i, [])
            p.blocks.append(cur)
            panic = True
            continue
        if _GO_SUMMARY.match(line):
            p.summary.append(i)
            cur, pending = None, []
            continue
        if not line.strip() and cur is not None:
            cur[1].append(i)
        elif line[:1] in (" ", "\t"):
            if cur is not None and _indent(line) > base:
                cur[1].append(i)
            else:
                cur = None
                pending.append(i)
        else:
            cur, pending = None, []
    p.blocks = [(h, _trim(b, lines)) for h, b in p.blocks]
    return p


def _parse_cargo(lines: list[str]) -> _Parsed:
    p = _Parsed()
    cur: tuple[int, list[int]] | None = None
    in_list = False
    ok = 0
    total = 0
    for i, line in enumerate(lines):
        if _CARGO_BLOCK.match(line):
            cur = (i, [])
            p.blocks.append(cur)
            in_list = False
            continue
        if line == "failures:":
            cur, in_list = None, True
            continue
        if line.startswith("test result:"):
            cur, in_list = None, False
            p.summary.append(i)
            m = _PASSED.search(line)
            total += int(m.group(1)) if m else 0
            continue
        t = _CARGO_TEST.match(line)
        if t:
            cur, in_list = None, False
            if t.group(2) == "ok":
                ok += 1
            elif t.group(2) == "FAILED":
                p.keep.append(i)
            continue
        if in_list and line.startswith("    "):
            p.keep.append(i)
        elif cur is not None:
            cur[1].append(i)
    p.passed = total or ok
    p.blocks = [(h, _trim(b, lines)) for h, b in p.blocks]
    return p


_PARSERS = {"pytest": _parse_pytest, "jest": _parse_jest, "go": _parse_go, "cargo": _parse_cargo}


class TestReportCompressor:
    __test__ = False  # not a pytest class despite the name

    name = "test_report"
    handles: frozenset[ContentType] = frozenset({"test_report"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("test_report", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        runner = _sniff(lines)
        if runner is None:
            return _TEXT.compress(text, level, ctx)
        p = _PARSERS[runner](lines)
        if not p.summary and not p.blocks and not p.keep:
            return _TEXT.compress(text, level, ctx)
        n = setting(f"test_report.block_lines.{arm(level)}", ctx)
        err = set(error_line_indices(text))
        covered = set(p.summary) | set(p.keep)
        for h, body in p.blocks:
            covered.add(h)
            covered.update(body)
        extras = [i for i in sorted(err) if i not in covered]

        out = [lines[i] for i in p.summary]
        kept = len(out)
        for h, body in p.blocks:
            out.append(lines[h])
            kept += 1 + self._block(lines, body, n, err, p.e_prefix, out)
        out.extend(lines[i] for i in extras)
        out.extend(lines[i] for i in p.keep)
        kept += len(extras) + len(p.keep)
        if p.passed and (p.blocks or p.keep or extras):
            out.append(f"[{p.passed} passed tests elided]")
        return _finish(text, out, trailer, kept, total)

    @staticmethod
    def _block(
        lines: list[str],
        body: list[int],
        n: int,
        err: set[int],
        e_prefix: str | None,
        out: list[str],
    ) -> int:
        pinned = {i for i in body if i in err}
        if e_prefix is not None:
            pinned.update([i for i in body if lines[i].startswith(e_prefix)][:n])
        return _select(lines, body, n, pinned, out)


# ------------------------------------------------------------------ compilers

_GCC = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^\s:(][^:]*):\d+(?::\d+)?: (?:fatal )?"
    r"(?P<sev>error|warning)\b"
)
_TSC = re.compile(r"^(?P<file>[^\s(][^(]*)\(\d+,\d+\): (?P<sev>error|warning) TS\d+")
_RUST = re.compile(r"^(?P<sev>error|warning)(?:\[\w+\])?: ")
_RUST_FILE = re.compile(r"^\s*--> (?P<file>.+?):\d+:\d+")
_RUST_TAIL = re.compile(
    r"^(?:error: (?:aborting|could not compile|build failed)|warning: \d+ warnings? emitted)"
)
_NOTE = re.compile(r"^(?:[^\s:][^:]*:\d+(?::\d+)?: (?:note|info)\b|(?:note|help)(?:\[\w+\])?: )")
_GUTTER = re.compile(r"^\d+ *\|")
SNIPPET_LINES = 6
_WARN_MODES = frozenset({"keep", "count"})


@dataclass
class _Diag:
    start: int
    end: int
    sev: str
    file: str | None


def _header(line: str) -> tuple[str, str | None] | None:
    if "error" not in line and "warning" not in line:
        return None
    m = _GCC.match(line) or _TSC.match(line)
    if m:
        return m.group("sev"), m.group("file")
    m2 = _RUST.match(line)
    if m2 and not _RUST_TAIL.match(line):
        return m2.group("sev"), None
    return None


class CompilerCompressor:
    name = "compiler"
    handles: frozenset[ContentType] = frozenset({"compiler"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("compiler", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        diags = self._scan(lines)
        if not diags:
            return _TEXT.compress(text, level, ctx)
        err = set(error_line_indices(text))
        collapse = choice(f"compiler.warnings.{arm(level)}", ctx, _WARN_MODES) == "count"

        groups: dict[str | None, list[_Diag]] = {}
        for d in diags:
            groups.setdefault(d.file, []).append(d)
        order = [f for f in groups if f is not None] + ([None] if None in groups else [])

        out: list[str] = []
        kept = 0
        covered: set[int] = set()
        for f in order:
            elided = 0
            for d in groups[f]:
                covered.update(range(d.start, d.end))
                if (
                    collapse
                    and d.sev == "warning"
                    and not any(i in err for i in range(d.start, d.end))
                ):
                    elided += 1
                    continue
                out.append(lines[d.start])
                kept += 1 + _select(
                    lines,
                    list(range(d.start + 1, d.end)),
                    SNIPPET_LINES,
                    {i for i in range(d.start + 1, d.end) if i in err},
                    out,
                )
            if elided:
                out.append(f"[{elided} warnings elided in {f or '(no file)'}]")
        extras = [i for i in sorted(err) if i not in covered]
        out.extend(lines[i] for i in extras)
        kept += len(extras)
        errors = sum(d.sev == "error" for d in diags)
        files = len({d.file for d in diags if d.file is not None})
        out.append(
            f"[token-sparen: {errors} errors, {len(diags) - errors} warnings in {files} files]"
        )
        return _finish(text, out, trailer, kept, total, force=True)

    @staticmethod
    def _scan(lines: list[str]) -> list[_Diag]:
        n = len(lines)
        diags: list[_Diag] = []
        i = 0
        while i < n:
            h = _header(lines[i])
            if h is None:
                i += 1
                continue
            j = i + 1
            while j < n:
                ln = lines[j]
                if ln[:1] in (" ", "\t") or _GUTTER.match(ln):
                    pass
                elif not ln or _header(ln) is not None or _NOTE.match(ln) is None:
                    break
                j += 1
            file = h[1]
            if file is None:
                for k in range(i + 1, min(j, i + 4)):
                    m = _RUST_FILE.match(lines[k])
                    if m:
                        file = m.group("file")
                        break
            diags.append(_Diag(i, j, h[0], file))
            i = j
        return diags


# ------------------------------------------------------------------ build logs

TAIL_LINES = 20
_BL_HEADER = re.compile(
    r"^(?:Step \d+/\d+\b|#\d+ \[|> Task :|npm (?:WARN|ERR!)|BUILD |Successfully |added \d+ packages"
    r"|> \S+@\S+ )"
)
_BL_PROGRESS = re.compile(
    r"\b\d{1,3}(?:\.\d+)?%"
    r"|^\s*(?:#\d+ [\d.]+ )?(?:Downloading|Downloaded|Download|Collecting|Using cached|Fetched"
    r"|Fetching|Pulling|Extracting|Resolving|Receiving|Unpacking|Get:|Hit:)\b"
    r"|\[[=>#. -]{4,}\]|[━█▓]{3,}|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
    r"|: (?:Pulling fs layer|Waiting|Download complete|Pull complete|Extracting|Verifying Checksum)"
)
_BL_WARN = re.compile(r"\bwarn(?:ing)?s?\b", re.IGNORECASE)


class BuildLogCompressor:
    name = "build_log"
    handles: frozenset[ContentType] = frozenset({"build_log"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("build_log", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        err = set(error_line_indices(text))
        tail_from = total - TAIL_LINES
        # items: (text, class) with class K keep, P progress, W warning, L plain
        items: list[tuple[str, str]] = []
        for i, line in enumerate(lines):
            if "\r" in line:
                segs = [s for s in line.split("\r") if s]
                for s in segs[:-1]:
                    if is_error_line(s):
                        items.append((s, "K"))
                line = segs[-1] if segs else ""
                bad = is_error_line(line)
            else:
                bad = i in err
            items.append((line, self._classify(line, bad, i >= tail_from)))
        for k in range(1, len(items)):
            if items[k][1] == "K" and items[k - 1][1] == "L":
                items[k - 1] = (items[k - 1][0], "K")
        out: list[str] = []
        gap = 0
        warns = 0
        kept = 0
        for line, cls in items:
            if cls in "KT":
                kept += 1
                if gap:
                    out.append(self._gap(gap, warns))
                    gap = warns = 0
                out.append(line)
            else:
                gap += 1
                warns += cls == "W"
        if gap:
            out.append(self._gap(gap, warns))
        return _finish(text, out, trailer, kept, total)

    @staticmethod
    def _gap(n: int, warns: int) -> str:
        if not warns:
            return elided_line(n)
        return f"… [{n} lines elided, {warns} warning{'s' if warns != 1 else ''}]"

    @staticmethod
    def _classify(line: str, bad: bool, in_tail: bool) -> str:
        if bad or _BL_HEADER.match(line):
            return "K"
        if _BL_PROGRESS.search(line):
            return "P"
        if _BL_WARN.search(line):
            return "W" if not in_tail else "T"
        return "T" if in_tail else "L"
