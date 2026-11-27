"""Render the README Evidence block from the published E4 evidence and enforce the release gate.

Reuses M5's validator (`bench.e4.load_e4`) and `scripts.plugin_hash.plugin_hash`; nothing about
the schema is duplicated here. Claims policy (spec section 10.5): every published number carries
its 95 % CI, n, seeds, model, date range and the plugin hash/version it measured.

Usage:
    python scripts/render_evidence.py --stdout [--evidence FILE]
    python scripts/render_evidence.py --write README.md [--evidence FILE]
    python scripts/render_evidence.py --check --tree DIR [--evidence FILE] [--decision FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.e4 import E4Error, load_e4  # noqa: E402
from scripts.plugin_hash import plugin_hash  # noqa: E402

START = "<!-- evidence:start -->"
END = "<!-- evidence:end -->"
DEFAULT_EVIDENCE = ROOT / "docs" / "evidence" / "e4.json"
DEFAULT_DECISION = ROOT / "docs" / "release" / "gate-decision.toml"
REPORT_LINK = "docs/evidence/e4.json"
PLACEHOLDER = (
    "No published benchmark results yet. Results appear here only from the live benchmark "
    "(E4), each with its 95 % CI, number of tasks, seeds, model, date and the plugin hash it "
    "measured."
)
CLAIMS_FIELDS = (
    "n_tasks",
    "seeds",
    "model",
    "claude_code_version",
    "date",
    "plugin_version",
    "plugin_hash",
    "harness_commit",
)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str


def _load(path: Path) -> dict[str, Any]:
    try:
        doc = load_e4(path)
    except OSError as exc:
        raise E4Error(f"{path}: cannot read ({exc.strerror})") from exc
    missing = [f for f in CLAIMS_FIELDS if f not in doc]
    if missing:  # defence in depth: the schema already requires them
        raise E4Error(f"{path}: claims fields missing: {', '.join(missing)}")
    return doc


def _num(x: float) -> str:
    return repr(float(x))


def render_evidence(path: Path | None) -> str:
    if path is None or not path.is_file():
        return PLACEHOLDER
    doc = _load(path)
    d = doc["date"]
    lines = [
        "| Metric | Arm vs baseline | Estimate | 95 % CI | n | Method |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in doc["results"]:
        lo, hi = r["ci95"]
        label = r["arm"] + (f" vs {r['baseline']}" if r.get("baseline") else "")
        kind = "confirmatory" if r["confirmatory"] else "descriptive"
        lines.append(
            f"| {r['metric']} ({kind}) | {label} | {_num(r['estimate'])} | "
            f"[{_num(lo)}, {_num(hi)}] | {r['n']} | {r['method']} |"
        )
    claims = (
        f"Measured on {doc['n_tasks']} tasks, {doc['seeds']} seeds, model `{doc['model']}`, "
        f"Claude Code {doc['claude_code_version']}, {d['from']} to {d['to']}. "
        f"Plugin version {doc['plugin_version']}, plugin hash `{doc['plugin_hash']}`, "
        f"harness commit `{doc['harness_commit']}`. Stage: {doc['stage']}. "
        f"Full report: [{REPORT_LINK}]({REPORT_LINK})."
    )
    return "\n".join(lines) + "\n\n" + claims


def replace_block(text: str, body: str) -> str:
    start, end = text.find(START), text.find(END)
    if start < 0 or end < start:
        raise ValueError(f"README lacks the {START} ... {END} block")
    return text[: start + len(START)] + "\n" + body + "\n" + text[end:]


def _plugin_version(tree: Path) -> str:
    manifest = json.loads((tree / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return str(manifest.get("version", ""))


def _decision_ok(tree: Path, decision: Path | None) -> str | None:
    """None if a valid decision file for this version exists, else the reason it does not."""
    if decision is None or not decision.is_file():
        return "underpowered and no gate-decision file"
    try:
        data = tomllib.loads(decision.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "gate-decision file is unreadable or not valid TOML"
    if data.get("version") != _plugin_version(tree):
        return "gate-decision file is not for this plugin version"
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return "gate-decision file has no rationale"
    return None


def check_release_gate(evidence: Path | None, tree: Path, decision: Path | None) -> GateResult:
    if evidence is None or not evidence.is_file():
        return GateResult(False, "no published E4 evidence")
    try:
        doc = _load(evidence)
    except E4Error as exc:
        return GateResult(False, f"invalid evidence: {exc}")
    actual = plugin_hash(tree)
    if actual != doc["plugin_hash"]:
        return GateResult(False, "plugin hash mismatch: evidence measured a different plugin")
    gate = doc["gate"]
    verdict = gate["verdict"] if gate else None
    if verdict == "passed":
        return GateResult(True, "gate passed")
    if verdict == "underpowered":
        problem = _decision_ok(tree, decision)
        if problem:
            return GateResult(False, problem)
        return GateResult(True, "underpowered, accepted by gate-decision file")
    if verdict == "failed":
        return GateResult(False, "gate failed (no override possible)")
    return GateResult(False, "no gate verdict in evidence")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    p.add_argument("--check", action="store_true")
    p.add_argument("--tree", type=Path)
    p.add_argument("--decision", type=Path, default=DEFAULT_DECISION)
    p.add_argument("--write", type=Path, metavar="README")
    p.add_argument("--stdout", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.check:
            if args.tree is None:
                p.error("--check needs --tree")
            res = check_release_gate(args.evidence, args.tree, args.decision)
            if not res.ok:
                print(f"release gate: {res.reason}", file=sys.stderr)
                return 1
            print(f"release gate: {res.reason}")
            return 0
        body = render_evidence(args.evidence)
        if args.write:
            text = args.write.read_text(encoding="utf-8")
            args.write.write_text(replace_block(text, body), encoding="utf-8")
            return 0
        print(body)
        return 0
    except (E4Error, ValueError, OSError) as exc:
        print(f"render_evidence: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
