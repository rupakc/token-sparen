"""Release checks: may this checkout be released (``tag``) or rehearsed (``dry-run``)?

Both modes: ``token_sparen.__version__`` equals ``.claude-plugin/plugin.json``; the CHANGELOG has
exactly one heading for the version; the `release/claude` tree builds and meets the directory
limits. ``tag`` mode adds: the tag matches ``^v\\d+\\.\\d+\\.\\d+$`` exactly (milestone and RC tags
such as ``v0.4.0-m4``, ``v0.5.0-screen1`` or ``v1.0.0-rc1`` never release) and equals ``v`` +
version; the CHANGELOG heading is dated (not ``Unreleased``); for ``X.Y.0`` the E4 release gate
(`render_evidence.check_release_gate`) passes on the built tree and the evidence's plugin hash
equals the built tree's; for a patch ``X.Y.Z`` (Z > 0) the patch rule (ruling N4) applies
instead. ``dry-run`` needs no tag and no evidence and skips the gate.

Owner override (ruling M5-B1, file format fixed by the M6 controller): only when the evidence's
gate verdict is ``underpowered`` (never ``failed``, never missing evidence) is a committed
``docs/release/gate-decision.toml`` handed to the gate, and only when it has ``version`` (= the
plugin.json version), ``decision = "ship"`` (``"hold"`` stops the release), a non-empty
``rationale``, an ISO ``date`` and ``e4_id`` (first 12 hex of sha256 of
``docs/evidence/e4.json``), and the commit that last changed it is signed by the owner's key
(``git log --format=%G?`` = ``G``; the verifying git must trust the owner's key).

There is deliberately no flag and no environment variable that skips the gate.

Usage: ``python scripts/release_check.py --mode tag --tag vX.Y.Z`` or ``--mode dry-run``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_claude_tree import TreeError, build_claude_tree  # noqa: E402
from scripts.plugin_hash import plugin_hash  # noqa: E402

Mode = Literal["tag", "dry-run"]
MODES = ("tag", "dry-run")
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")  # used with fullmatch: no trailing newline
RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
DEV_VERSION = re.compile(r"\d+\.\d+\.\d+\.dev\d+")
HEADING = re.compile(r"^## \[(?P<v>[^\]]+)\](?: - (?P<d>.*?))?\s*$", re.MULTILINE)
INIT_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
EVIDENCE = Path("docs/evidence/e4.json")
DECISION = Path("docs/release/gate-decision.toml")
TREATMENT_ARM = "token-sparen"
RUNTIME_HEADING = "### Runtime changes"
# a change below these needs a new E4 confirmation (defaults or learning behaviour)
CONFIRMATION_PATHS = ("token_sparen/params/", "token_sparen/learn/")
UNRELEASED = "Unreleased"
DECISION_KEYS = frozenset({"version", "decision", "rationale", "date", "e4_id"})


def is_release_tag(tag: str) -> bool:
    """True only for ``vX.Y.Z`` (never an RC, milestone or screening tag)."""
    return RELEASE_TAG.fullmatch(tag) is not None


def _load_gate() -> Callable[..., Any]:
    """`check_release_gate(evidence, tree, decision) -> GateResult(ok, reason)` (M6 Task 6)."""
    from scripts.render_evidence import check_release_gate

    return check_release_gate  # type: ignore[no-any-return]


def _load_e4() -> Callable[[Path], dict[str, Any]]:
    from bench.e4 import load_e4

    return load_e4


def _versions(root: Path) -> tuple[str | None, list[str]]:
    problems: list[str] = []
    init = root / "token_sparen" / "__init__.py"
    match = INIT_VERSION.search(init.read_text(encoding="utf-8")) if init.is_file() else None
    if not match:
        return None, [f"no __version__ in {init.relative_to(root).as_posix()}"]
    version = match.group(1)
    try:
        plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return version, [f".claude-plugin/plugin.json unreadable: {exc}"]
    if plugin.get("version") != version:
        problems.append(
            f".claude-plugin/plugin.json version {plugin.get('version')!r} != "
            f"token_sparen.__version__ {version!r} (run scripts/bump_version.py)"
        )
    if not (RELEASE_VERSION.fullmatch(version) or DEV_VERSION.fullmatch(version)):
        problems.append(f"version {version!r} is neither X.Y.Z nor X.Y.Z.devN")
    return version, problems


def _headings(changelog: str) -> list[tuple[str, str | None]]:
    return [(m["v"], m["d"]) for m in HEADING.finditer(changelog)]


def _changelog(root: Path, version: str, mode: Mode) -> tuple[str, list[str]]:
    path = root / "CHANGELOG.md"
    if not path.is_file():
        return "", ["CHANGELOG.md missing"]
    text = path.read_text(encoding="utf-8")
    headings = _headings(text)
    problems: list[str] = []
    names = [v for v, _ in headings]
    dupes = sorted({v for v in names if names.count(v) > 1})
    if dupes:
        problems.append(f"CHANGELOG.md: duplicate heading(s) {dupes}")
    if DEV_VERSION.fullmatch(version):
        if "Unreleased" not in names:
            problems.append("CHANGELOG.md: a development version needs a ## [Unreleased] heading")
        return text, problems
    mine = [d for v, d in headings if v == version]
    if len(mine) != 1:
        problems.append(f"CHANGELOG.md: needs exactly one heading for {version}, found {len(mine)}")
        return text, problems
    date = mine[0] or ""
    if mode == "tag" and not _is_date(date):
        problems.append(
            f"CHANGELOG.md: the {version} heading must be dated (YYYY-MM-DD) for a release, "
            f"found {date or 'no date'!r} (bump_version.py --set-date; Unreleased passes "
            "only until the release)"
        )
    elif mode == "dry-run" and date != UNRELEASED and not _is_date(date):
        problems.append(f"CHANGELOG.md: {version} heading date {date!r} is not YYYY-MM-DD")
    return text, problems


def _is_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def section(changelog: str, version: str) -> str:
    """The body below ``## [version]`` up to the next ``## `` heading or the milestone marker."""
    matches = [m for m in HEADING.finditer(changelog) if m["v"] == version]
    if not matches:
        return ""
    start = matches[0].end()
    rest = changelog[start:]
    nxt = re.search(r"^## |^<!-- pre-1\.0 milestones -->", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _evidence(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / EVIDENCE
    if not path.is_file():
        return None, f"no E4 evidence at {EVIDENCE.as_posix()}"
    try:
        return _load_e4()(path), None
    except (ValueError, OSError) as exc:
        return None, f"E4 evidence invalid: {exc}"


def _measured_ref(doc: dict[str, Any]) -> str | None:
    for arm in doc.get("arms", []):
        if isinstance(arm, dict) and arm.get("name") == TREATMENT_ARM and arm.get("pin"):
            return str(arm["pin"])
    return None


def _decision_content(data: dict[str, Any], root: Path, version: str, where: str) -> list[str]:
    problems: list[str] = []
    unknown = sorted(set(data) - DECISION_KEYS)
    if unknown:
        problems.append(f"{where}: unknown key(s) {unknown}")
    missing = sorted(DECISION_KEYS - set(data))
    if missing:
        problems.append(f"{where}: missing key(s) {missing}")
    if "version" in data and data["version"] != version:
        problems.append(f"{where}: version {data['version']!r} != plugin.json {version!r}")
    if data.get("decision", "ship") == "hold":
        problems.append(f"{where}: the owner decided 'hold'; no release")
    elif data.get("decision", "ship") != "ship":
        problems.append(f"{where}: decision must be 'ship' or 'hold', got {data['decision']!r}")
    rationale = data.get("rationale", "x")
    if not isinstance(rationale, str) or not rationale.strip():
        problems.append(f"{where}: rationale must be a non-empty string")
    date = data.get("date", "2000-01-01")
    plain_date = isinstance(date, dt.date) and not isinstance(date, dt.datetime)
    if not (plain_date or (isinstance(date, str) and _is_date(date))):
        problems.append(f"{where}: date must be an ISO date (YYYY-MM-DD), got {date!r}")
    evidence = root / EVIDENCE
    if "e4_id" in data and evidence.is_file():
        e4_id = hashlib.sha256(evidence.read_bytes()).hexdigest()[:12]
        if data["e4_id"] != e4_id:
            problems.append(
                f"{where}: e4_id {data['e4_id']!r} != {e4_id} (decided on other evidence)"
            )
    return problems


def decision_problems(root: Path, version: str) -> list[str]:
    """Why ``docs/release/gate-decision.toml`` cannot override an ``underpowered`` gate."""
    path = root / DECISION
    where = DECISION.as_posix()
    if path.is_symlink() or not path.is_file():
        return [f"gate verdict underpowered: no owner decision file {where}"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [f"{where}: invalid TOML ({exc})"]
    problems = _decision_content(data, root, version, where)
    dirty = _git(root, "status", "--porcelain", "--", where)
    if dirty.returncode != 0 or dirty.stdout.strip():
        problems.append(f"{where}: not committed as is (uncommitted changes)")
    last = _git(root, "log", "-1", "--format=%H %G?", "--", where)
    fields = last.stdout.split()
    if last.returncode != 0 or not fields:
        problems.append(f"{where}: never committed; the owner commits it signed")
    elif fields[1:] != ["G"]:
        status = fields[1] if len(fields) > 1 else "?"
        problems.append(
            f"{where}: commit {fields[0][:12]} is not signed by a trusted owner key "
            f"(git %G? = {status!r}, needs 'G')"
        )
    return problems


def _gate(root: Path, tree: Path, version: str) -> list[str]:
    """X.Y.0: the E4 release gate on the built tree. No skip exists."""
    try:
        check = _load_gate()
    except ImportError as exc:
        return [f"release gate unavailable ({exc}); a tag release never skips the gate"]
    evidence = root / EVIDENCE
    doc, error = _evidence(root)
    gate = doc.get("gate") if doc is not None else None
    verdict = gate.get("verdict") if isinstance(gate, dict) else None
    problems: list[str] = []
    decision: Path | None = None
    if verdict == "underpowered":
        override = decision_problems(root, version)
        if override:
            problems += [f"release gate: {p}" for p in override]
        else:
            decision = root / DECISION
    result = check(evidence if evidence.is_file() else None, tree, decision)
    if not result.ok:
        problems.append(f"release gate: {result.reason}")
    if doc is None:
        problems.append(f"release gate: {error}")
    else:
        built = plugin_hash(tree)
        if doc.get("plugin_hash") != built:
            problems.append(
                f"release gate: evidence plugin hash {str(doc.get('plugin_hash'))[:12]} != "
                f"built tree plugin hash {built[:12]} (measured a different plugin)"
            )
    return problems


def _patch_rule(root: Path, version: str, changelog: str) -> list[str]:
    """X.Y.Z with Z > 0 (ruling N4): (a) README states the measured version and hash, (b) the
    section lists runtime changes, (c) nothing under params/ or learn/ changed since the
    measured ref."""
    doc, error = _evidence(root)
    if doc is None:
        return [f"patch rule: {error} (a patch ships on the last measured evidence)"]
    problems: list[str] = []
    ref = _measured_ref(doc) or str(doc.get("plugin_version", ""))
    digest = str(doc.get("plugin_hash", ""))[:12]
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    if f"measured on {ref}".lower() not in text.lower() or f"plugin {digest}" not in text:
        problems.append(
            f"patch rule (a): README Evidence must state 'measured on {ref}, plugin {digest}'"
        )
    body = section(changelog, version)
    runtime = re.search(
        rf"^{re.escape(RUNTIME_HEADING)}\s*$(?P<b>.*?)(?=^#|\Z)", body, re.MULTILINE | re.S
    )
    listed = runtime and re.search(r"^\s*[-*] \S|^\s*none\s*$", runtime["b"], re.M | re.I)
    if not listed:
        problems.append(
            f"patch rule (b): the {version} CHANGELOG section needs a '{RUNTIME_HEADING}' list "
            "(or 'none')"
        )
    verify = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if verify.returncode != 0:
        problems.append(f"patch rule (c): cannot resolve the measured ref {ref!r}")
        return problems
    diff = _git(root, "diff", "--name-only", f"{verify.stdout.strip()}..HEAD")
    if diff.returncode != 0:
        problems.append(f"patch rule (c): git diff failed: {diff.stderr.strip()}")
        return problems
    for rel in diff.stdout.splitlines():
        if rel.startswith(CONFIRMATION_PATHS):
            problems.append(
                f"patch rule (c): {rel} changed since {ref}: defaults or learning behaviour "
                "changed, a new E4 confirmation is required"
            )
    return problems


def release_check(root: Path, *, tag: str | None, mode: Mode) -> list[str]:
    """Every reason ``root`` cannot be released (``tag``) or rehearsed (``dry-run``)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    root = root.resolve()
    version, problems = _versions(root)
    changelog = ""
    if version is not None:
        changelog, more = _changelog(root, version, mode)
        problems += more
    with tempfile.TemporaryDirectory(prefix="token-sparen-tree-") as tmp:
        tree = Path(tmp) / "tree"
        try:
            build_claude_tree(root, tree)
        except (OSError, TreeError) as exc:
            problems.append(f"release/claude tree: {exc}")
            tree_ok = False
        else:
            tree_ok = True
        if mode == "dry-run" or version is None:
            return problems
        if tag is None:
            return [*problems, "tag mode needs --tag vX.Y.Z"]
        if not is_release_tag(tag):
            problems.append(f"{tag!r} is not a release tag (only vX.Y.Z releases)")
        elif tag != f"v{version}":
            problems.append(f"tag {tag} does not match the version {version} (expected v{version})")
        release = RELEASE_VERSION.fullmatch(version)
        if release is None:
            return [*problems, f"version {version} is a development version; never released"]
        if not tree_ok:
            return problems
        if int(release.group(3)) == 0:
            problems += _gate(root, tree, version)
        else:
            problems += _patch_rule(root, version, changelog)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_check", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--tag", help="the pushed tag (tag mode), e.g. v1.0.0")
    parser.add_argument("--root", default=ROOT, type=Path, help="repository root")
    args = parser.parse_args(argv)
    problems = release_check(args.root, tag=args.tag, mode=args.mode)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok release check ({args.mode}{' ' + args.tag if args.tag else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
