"""E2 fact-preservation suite: compress a corpus of tool outputs, check every needle survives.

Mirrors production per case and level: detect (optional command hint) -> registry compressor
-> if lossy, save the full text under a temporary project dir and append the marker. A needle
passes if it is in that final text, or (unless tagged ``must_inline``) in the recovery file.

    python evals/e2/run.py [--strict] [--json] [--params OVERLAY.toml]

``run(levels, params)`` evaluates a config overlay (written as the isolated home's user
config.toml) and reports ``inline_rate`` per level: needles visible in the compressed text itself.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python evals/e2/run.py` from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from token_sparen import recovery
from token_sparen.compress import CompressContext, detect, load_level, registry
from token_sparen.config import _MISSING, _dig, _leaves, _same_type, load_config, packaged_defaults
from token_sparen.estimate import estimate_tokens
from token_sparen.events import ToolCall

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
NEEDLES = HERE / "needles.toml"
LEVELS = ("light", "standard", "aggressive")
MIN_TOKENS = 2000  # outputs below this are never modified in production
MAX_MEAN_RATIO = 0.6


@dataclass(frozen=True)
class Case:
    file: str
    text: str
    command: str | None
    types: tuple[str, ...]
    needles: tuple[str, ...]
    must_inline: tuple[str, ...]
    tokens: int


@dataclass
class E2Report:
    cases: int  # case x level runs
    passed: int
    failures: list[tuple[str, str, str]]  # (case, level, needle)
    mean_ratio: dict[str, float]  # mean compressed/original tokens over compressible cases
    rows: list[dict[str, object]] = field(default_factory=list)
    inline_rate: dict[str, float] = field(default_factory=dict)  # needles in the text itself


def load_cases() -> list[Case]:
    raw = tomllib.loads(NEEDLES.read_text(encoding="utf-8"))
    out: list[Case] = []
    for c in raw["case"]:
        text = (CORPUS / c["file"]).read_text(encoding="utf-8")
        out.append(
            Case(
                file=c["file"],
                text=text,
                command=c.get("command"),
                types=tuple(c["types"]),
                needles=tuple(c["needles"]),
                must_inline=tuple(c.get("must_inline", ())),
                tokens=estimate_tokens(text),
            )
        )
    return out


def needle_ok(needle: str, final: str, recovered: str | None, *, must_inline: bool) -> bool:
    if needle in final:
        return True
    return not must_inline and recovered is not None and needle in recovered


def _detected(case: Case) -> str:
    tool = None
    if case.command:
        tool = ToolCall(name="Bash", category="shell", input={"command": case.command}, use_id=None)
    return detect(case.text, tool)


def _compress_case(case: Case, level: str, tmp: Path) -> tuple[str, str | None, float]:
    """Return (final text, recovery file text or None, compressed/original token ratio)."""
    if case.tokens < MIN_TOKENS:
        return case.text, None, 1.0
    cfg = load_config(tmp, {"TOKEN_SPAREN_HOME": str(tmp / "home")})
    comp = registry.for_type(_detected(case))  # type: ignore[arg-type]
    res = comp.compress(case.text, load_level(cfg, level), CompressContext(None, (), cfg))
    final, recovered = res.text, None
    if not res.lossless:
        ref = recovery.save_full(tmp, case.text)
        final = res.text + "\n" + recovery.marker(res.kept_lines, res.total_lines, ref)
        recovered = ref.path.read_text(encoding="utf-8")
    return final, recovered, estimate_tokens(final) / max(1, case.tokens)


@contextlib.contextmanager
def _isolated_home(tmp: Path) -> Iterator[None]:
    old = os.environ.get("TOKEN_SPAREN_HOME")
    os.environ["TOKEN_SPAREN_HOME"] = str(tmp / "home")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TOKEN_SPAREN_HOME", None)
        else:
            os.environ["TOKEN_SPAREN_HOME"] = old


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


def _toml_dump(node: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> str:
    """Minimal TOML writer for an overlay (nested tables of scalars and string lists)."""
    out = [f"{k} = {_toml_value(v)}" for k, v in node.items() if not isinstance(v, Mapping)]
    text = "\n".join(out) + "\n" if out else ""
    for k, v in node.items():
        if isinstance(v, Mapping):
            path = (*prefix, str(k))
            text += f"\n[{'.'.join(path)}]\n" + _toml_dump(v, path)
    return text


def validate_overlay(params: Mapping[str, Any]) -> None:
    """Raise ValueError unless every overlay leaf is a packaged-default key of the right type."""
    defaults = packaged_defaults()
    for path, value in _leaves(dict(params), ()):
        key = ".".join(path)
        base = _dig(defaults, path)
        if base is _MISSING:
            raise ValueError(f"params overlay: unknown key {key!r}")
        if not _same_type(value, base):
            raise ValueError(
                f"params overlay: {key!r} must be {type(base).__name__}, got {value!r}"
            )


def run(levels: Sequence[str] = LEVELS, params: Mapping[str, Any] | None = None) -> E2Report:
    cases = load_cases()
    if params:
        validate_overlay(params)
    failures: list[tuple[str, str, str]] = []
    rows: list[dict[str, object]] = []
    ratios: dict[str, list[float]] = {lv: [] for lv in levels}
    inline = {lv: 0 for lv in levels}
    total = 0
    passed = 0
    with tempfile.TemporaryDirectory(prefix="ts-e2-") as d, _isolated_home(Path(d)):
        tmp = Path(d)
        if params:
            (tmp / "home").mkdir(parents=True, exist_ok=True)
            (tmp / "home" / "config.toml").write_text(_toml_dump(params), encoding="utf-8")
        total = sum(len(c.needles) for c in cases)
        for case in cases:
            kind = _detected(case)
            for lv in levels:
                bad: list[str] = []
                if kind not in case.types:
                    bad.append(f"<detect: expected {'|'.join(case.types)}, got {kind}>")
                try:
                    final, recovered, ratio = _compress_case(case, lv, tmp)
                except Exception as exc:  # noqa: BLE001 - a crash is a failure, not an abort
                    final, recovered, ratio = "", None, 1.0
                    bad.append(f"<exception: {type(exc).__name__}: {exc}>")
                inline[lv] += sum(n in final for n in case.needles)
                for n in case.needles:
                    if not needle_ok(n, final, recovered, must_inline=n in case.must_inline):
                        bad.append(n)
                failures.extend((case.file, lv, n) for n in bad)
                passed += not bad
                if case.tokens >= MIN_TOKENS:
                    ratios[lv].append(ratio)
                rows.append(
                    {"case": case.file, "level": lv, "ok": not bad, "ratio": round(ratio, 3)}
                )
    mean = {lv: (sum(r) / len(r) if r else 1.0) for lv, r in ratios.items()}
    rate = {lv: (inline[lv] / total if total else 0.0) for lv in levels}
    return E2Report(len(cases) * len(levels), passed, failures, mean, rows, rate)


def _table(report: E2Report, levels: Sequence[str]) -> str:
    by_case: dict[str, dict[str, dict[str, object]]] = {}
    for r in report.rows:
        by_case.setdefault(str(r["case"]), {})[str(r["level"])] = r
    width = max((len(c) for c in by_case), default=4)
    head = f"{'case':<{width}}  " + "  ".join(f"{lv:>16}" for lv in levels)
    lines = [head, "-" * len(head)]
    for case, cells in by_case.items():
        parts = []
        for lv in levels:
            r = cells[lv]
            parts.append(f"{'PASS' if r['ok'] else 'FAIL':>4} {float(str(r['ratio'])):>10.3f}   ")
        lines.append(f"{case:<{width}}  " + "  ".join(parts))
    lines.append("")
    lines.append(f"passed {report.passed}/{report.cases} case-level runs")
    lines.append("mean ratio: " + ", ".join(f"{k}={v:.3f}" for k, v in report.mean_ratio.items()))
    for case, lv, needle in report.failures:
        lines.append(f"FAIL {case} [{lv}] missing: {needle!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e2")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any failure or weak ratio")
    ap.add_argument("--params", type=Path, help="TOML overlay of config params to evaluate")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    params = tomllib.loads(args.params.read_text(encoding="utf-8")) if args.params else None
    report = run(params=params)
    if args.json:
        print(json.dumps(report.__dict__, indent=2))
    else:
        print(_table(report, LEVELS))
    if args.strict:
        if report.failures or report.passed != report.cases:
            return 1
        if report.mean_ratio.get("standard", 1.0) >= MAX_MEAN_RATIO:
            print(f"mean ratio at standard >= {MAX_MEAN_RATIO}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
