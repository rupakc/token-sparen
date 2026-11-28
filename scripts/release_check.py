"""Release checks: may this checkout be released (``tag``) or rehearsed (``dry-run``)?

Both modes: ``token_sparen.__version__`` equals ``.claude-plugin/plugin.json``; the CHANGELOG has
exactly one heading for the version; the `release/claude` tree builds and meets the directory
limits. ``tag`` mode adds: the tag matches ``^v\\d+\\.\\d+\\.\\d+$`` exactly (milestone and RC tags
such as ``v0.4.0-m4``, ``v0.5.0-screen1`` or ``v1.0.0-rc1`` never release) and equals ``v`` +
version; the CHANGELOG heading is dated (not ``Unreleased``); for ``X.Y.0`` (and for a patch
whose committed evidence measured that patch itself) the E4 release gate
(`render_evidence.check_release_gate`) passes on the built tree and the evidence's plugin hash
equals the built tree's; for a patch ``X.Y.Z`` (Z > 0) the patch rule (ruling N4, amended by
C1/I1/I2) applies instead: the reused evidence measured an earlier release of the same
MAJOR.MINOR, its verdict is ``passed`` (or ``underpowered`` with a valid signed ``ship``
decision for that evidence), the README states the measured version and hash (as
``render_evidence`` writes them), the CHANGELOG lists the runtime changes, and nothing but
non-behaviour files changed between the measured commit and the release commit (review M1: an
allow-list, `behaviour_changes`; everything else is behaviour). Tag mode also requires a clean
work tree and a marketplace entry that installs the ``release/claude`` tree (ruling B1,
`marketplace_problems`); ``dry-run`` requires that too once the CHANGELOG has a dated release
heading (`marketplace_required`). ``dry-run`` needs no tag and no evidence and skips the gate.

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
from scripts.bump_version import DEV_SOURCE, PLUGIN_NAME, release_source  # noqa: E402
from scripts.plugin_hash import PLUGIN_PATHS, plugin_files, plugin_hash  # noqa: E402
from scripts.render_evidence import check_release_gate, e4_id  # noqa: E402

Mode = Literal["tag", "dry-run"]
MODES = ("tag", "dry-run")
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")  # used with fullmatch: no trailing newline
RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
DEV_VERSION = re.compile(r"\d+\.\d+\.\d+\.dev\d+")
HEADING = re.compile(r"^## \[(?P<v>[^\]]+)\](?: - (?P<d>.*?))?\s*$", re.MULTILINE)
INIT_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
# byte-level twins for the normalised plugin hash: every other byte (line endings included) is
# hashed as it is, so the result is the same on every OS for the same bytes
_INIT_VERSION_B = re.compile(rb'^(__version__ = )"[^"\r\n]+"(?=\r?$)', re.MULTILINE)
_MANIFEST_VERSION_B = re.compile(rb'("version"\s*:\s*)"(?:[^"\\\r\n]|\\.)*"')
EVIDENCE = Path("docs/evidence/e4.json")
DECISION = Path("docs/release/gate-decision.toml")
TREATMENT_ARM = "token-sparen"
RUNTIME_HEADING = "### Runtime changes"
# Review M1 (rule inverted): every path changed between the measured commit and a patch release
# commit is behaviour, and needs a new E4 confirmation, unless it is on this allow-list and
# outside the hashed plugin content (`PLUGIN_PATHS`). Only the version line may change in the
# two version files (`VERSION_FILES`).
NON_BEHAVIOUR_PATHS = ("docs/", "tests/", "bench/", "evals/", "scripts/")
NON_BEHAVIOUR_FILES = frozenset(
    {
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "NOTICE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "CLAUDE.md",
        "MEMORY.md",
        # distribution metadata, unhashed and not shipped; the release-prep commit (ruling B1)
        # points it at release/claude
        ".claude-plugin/marketplace.json",
    }
)
VERSION_FILES = ("token_sparen/__init__.py", ".claude-plugin/plugin.json")
MARKETPLACE = Path(".claude-plugin/marketplace.json")
MILESTONE_MARKER = "<!-- pre-1.0 milestones -->"
UNRELEASED = "Unreleased"
DECISION_KEYS = frozenset({"version", "decision", "rationale", "date", "e4_id"})


def is_release_tag(tag: str) -> bool:
    """True only for ``vX.Y.Z`` (never an RC, milestone or screening tag)."""
    return RELEASE_TAG.fullmatch(tag) is not None


def _load_gate() -> Callable[..., Any]:
    """`render_evidence.check_release_gate` (a seam for tests; a gate that cannot be loaded is
    a release problem, never a skip)."""
    return check_release_gate


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
        expected = e4_id(evidence)
        if data["e4_id"] != expected:
            problems.append(
                f"{where}: e4_id {data['e4_id']!r} != {expected} (decided on other evidence)"
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
    # --full-history: a merge whose resolution changed the file is not simplified away.
    # %G? = G means "good signature by a key this git trusts": the release job must trust only
    # the owner's key (gpg keyring or gpg.ssh.allowedSignersFile; Task 10).
    last = _git(root, "log", "-1", "--full-history", "--format=%H %G?", "--", where)
    fields = last.stdout.split()
    if last.returncode != 0 or not fields:
        problems.append(f"{where}: never committed; the owner commits it signed")
        return problems
    if fields[1:] != ["G"]:
        status = fields[1] if len(fields) > 1 else "?"
        problems.append(
            f"{where}: commit {fields[0][:12]} is not signed by a trusted owner key "
            f"(git %G? = {status!r}, needs 'G')"
        )
    signed = _git(root, "rev-parse", "--verify", "--quiet", f"{fields[0]}:{where}")
    head = _git(root, "rev-parse", "--verify", "--quiet", f"HEAD:{where}")
    if signed.returncode != 0 or head.returncode != 0 or signed.stdout != head.stdout:
        problems.append(
            f"{where}: the file at HEAD is not the one in signed commit {fields[0][:12]}"
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


def _reusable(root: Path, version: str, doc: dict[str, Any]) -> list[str]:
    """Rulings C1/I1: the prior evidence was releasable and measured this MAJOR.MINOR."""
    problems: list[str] = []
    measured = str(doc.get("plugin_version", ""))
    m = RELEASE_VERSION.fullmatch(measured)
    r = RELEASE_VERSION.fullmatch(version)
    assert r is not None  # release_check only routes X.Y.Z here
    if m is None or m.group(1, 2) != r.group(1, 2) or int(m.group(3)) >= int(r.group(3)):
        problems.append(
            f"patch rule: the evidence measured {measured!r}, not an earlier {r[1]}.{r[2]}.x; "
            f"a new minor or major ({r[1]}.{r[2]}) needs its own E4 confirmation"
        )
    gate = doc.get("gate")
    verdict = gate.get("verdict") if isinstance(gate, dict) else None
    if verdict == "underpowered" and m is not None:
        override = decision_problems(root, measured)
        problems += [
            f"patch rule: underpowered evidence without a valid decision: {p}" for p in override
        ]
    elif verdict != "passed":
        problems.append(
            f"patch rule: the reused evidence's gate verdict is {verdict!r} (needs 'passed', or "
            "'underpowered' with a signed 'ship' decision); a new E4 run is required"
        )
    return problems


def _measures(root: Path, version: str) -> bool:
    """True when the committed evidence measured ``version`` itself: a patch with behaviour
    changes that got its own E4 run is judged by the full gate, like an ``X.Y.0``."""
    doc, _ = _evidence(root)
    return doc is not None and doc.get("plugin_version") == version


def _normalise_manifest(data: bytes) -> bytes:
    """``plugin.json`` with only its top-level ``version`` value replaced by ``"0"``, byte for
    byte otherwise; raises `ValueError` when it is not JSON. A manifest whose version cannot be
    located as exactly one ``"version": "..."`` pair is re-serialised canonically instead."""
    manifest = json.loads(data.decode("utf-8"))
    if not isinstance(manifest, dict) or "version" not in manifest:
        return data
    if len(_MANIFEST_VERSION_B.findall(data)) == 1:
        out = _MANIFEST_VERSION_B.sub(rb'\1"0"', data)
        check = json.loads(out.decode("utf-8"))
        if check == {**manifest, "version": "0"}:
            return out
    manifest["version"] = "0"
    return json.dumps(manifest, sort_keys=True).encode("utf-8")


def normalised_plugin_hash(tree: Path) -> str:
    """`plugin_hash` of a built tree with its version taken out: ``version`` in
    ``.claude-plugin/plugin.json`` and ``__version__`` in ``token_sparen/__init__.py`` are
    replaced by ``"0"`` (review I3), so two trees that differ only in their version match."""
    digest = hashlib.sha256()
    for rel in plugin_files(tree):
        data = (tree / rel).read_bytes()  # bytes as shipped: no newline or encoding translation
        if rel == VERSION_FILES[1]:
            data = _normalise_manifest(data)
        elif rel == VERSION_FILES[0]:
            data = _INIT_VERSION_B.sub(rb'\1"0"', data)
        digest.update(f"{rel}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return digest.hexdigest()


def tree_reuse_problems(root: Path, measured: str, tree: Path, evidence_hash: str) -> list[str]:
    """Review I3, authoritative for patch reuse: the measured commit, built by this checkout's
    tree builder, must still give the evidence's plugin hash (else the builder or the ref
    changed what ships), and the release tree must equal it once the version is normalised."""
    with tempfile.TemporaryDirectory(prefix="token-sparen-measured-") as tmp:
        old = Path(tmp) / "tree"
        try:
            build_claude_tree(root, old, ref=measured)
        except (OSError, TreeError) as exc:
            return [f"patch rule (d): cannot build the measured tree at {measured[:12]}: {exc}"]
        problems: list[str] = []
        if plugin_hash(old) != evidence_hash:
            problems.append(
                f"patch rule (d): the measured commit now builds plugin {plugin_hash(old)[:12]}, "
                f"not the evidence's {evidence_hash[:12]} (the tree builder or the measured ref "
                "changed); a new E4 confirmation is required"
            )
        try:
            same = normalised_plugin_hash(old) == normalised_plugin_hash(tree)
        except ValueError as exc:  # a plugin.json that is not JSON (fails closed, no traceback)
            return [*problems, f"patch rule (d): cannot normalise the plugin tree: {exc}"]
        if not same:
            problems.append(
                "patch rule (d): the release plugin tree differs from the measured one beyond "
                "the version; a new E4 confirmation is required"
            )
    return problems


RELEASE_BRANCH_REF = "refs/remotes/origin/release/claude"
SEED_FIX = (
    "push a corrective fast-forward commit whose tree is the measured plugin tree "
    "(docs/release/runbook.md, 'A wrong seed'), then check again"
)


def release_branch_problems(
    root: Path, version: str, *, ref: str = RELEASE_BRANCH_REF
) -> list[str]:
    """Review NEW-I1: when ``release/claude`` (``ref``, fetched from origin) already holds
    ``version`` (the first release's seed, made by hand), its plugin tree must be the measured
    one: equal, with the version normalised, to the tree of the commit the evidence measured,
    which itself must still build the evidence's plugin hash. A branch holding an older release
    is fine (``push-tree`` moves it forward); a missing ref is a problem (fetch it first)."""
    resolved = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if resolved.returncode != 0:
        return [
            f"{ref} not found: fetch it first (git fetch origin "
            "+refs/heads/release/claude:refs/remotes/origin/release/claude); a release needs "
            "the seeded or the previous release branch"
        ]
    head = resolved.stdout.strip()
    shown = _git(root, "show", f"{head}:{VERSION_FILES[1]}")
    try:
        held = json.loads(shown.stdout).get("version") if shown.returncode == 0 else None
    except (ValueError, AttributeError):
        held = None
    if held is None:
        return [f"release/claude ({head[:12]}) holds no readable {VERSION_FILES[1]}"]
    if held != version:
        return []
    doc, error = _evidence(root)
    if doc is None:
        return [f"release/claude holds {version}, but {error}: cannot check the seed"]
    measured_ref = _measured_ref(doc) or ""
    measured = _git(root, "rev-parse", "--verify", "--quiet", f"{measured_ref}^{{commit}}")
    if not measured_ref or measured.returncode != 0:
        return [f"release/claude seed check: cannot resolve the measured ref {measured_ref!r}"]
    evidence_hash = str(doc.get("plugin_hash", ""))
    with tempfile.TemporaryDirectory(prefix="token-sparen-seed-") as tmp:
        seed, old = Path(tmp) / "seed", Path(tmp) / "measured"
        try:
            build_claude_tree(root, seed, ref=head)
            build_claude_tree(root, old, ref=measured.stdout.strip())
            if plugin_hash(old) != evidence_hash:
                return [
                    f"release/claude seed check: {measured_ref} now builds plugin "
                    f"{plugin_hash(old)[:12]}, not the evidence's {evidence_hash[:12]}"
                ]
            same = normalised_plugin_hash(seed) == normalised_plugin_hash(old)
        except (OSError, TreeError, ValueError) as exc:
            return [f"release/claude seed check: cannot build or hash the trees: {exc}"]
    if not same:
        return [
            f"release/claude ({head[:12]}) holds {version} but not the measured plugin "
            f"{evidence_hash[:12]} of {measured_ref}: the marketplace would serve an unmeasured "
            f"tree; {SEED_FIX}"
        ]
    return []


def _patch_rule(root: Path, version: str, changelog: str, tree: Path) -> list[str]:
    """X.Y.Z with Z > 0 (ruling N4 as amended by C1/I1/I2): the reused evidence was releasable
    and measured this MAJOR.MINOR; (a) README states the measured version and hash, (b) the
    section lists runtime changes, (c) no behaviour file (`behaviour_changes`) changed between
    the measured commit and the release commit (a fast filter), (d) the release plugin tree
    equals the measured one up to the version (`tree_reuse_problems`, authoritative)."""
    doc, error = _evidence(root)
    if doc is None:
        return [f"patch rule: {error} (a patch ships on the last measured evidence)"]
    problems = _reusable(root, version, doc)
    ref = _measured_ref(doc) or str(doc.get("plugin_version", ""))
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    # review m-R1: the phrase `render_evidence --write README.md` emits for this evidence
    stated = (
        f"Plugin version {doc.get('plugin_version', '')}, "
        f"plugin hash `{doc.get('plugin_hash', '')}`"
    )
    if stated not in text:
        problems.append(
            f"patch rule (a): README Evidence must state the measured evidence ('{stated}'; "
            "run scripts/render_evidence.py --write README.md)"
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
    measured = verify.stdout.strip()
    problems += tree_reuse_problems(root, measured, tree, str(doc.get("plugin_hash", "")))
    try:
        changed = behaviour_changes(root, measured)
    except ValueError as exc:
        problems.append(f"patch rule (c): git diff failed: {exc}")
        return problems
    for rel in changed:
        problems.append(
            f"patch rule (c): {rel} changed since {ref}: behaviour changed (only docs, tests, "
            "bench, evals, unshipped scripts, the listed top-level texts and the version line "
            "may change), a new E4 confirmation is required"
        )
    return problems


def _in_plugin(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PLUGIN_PATHS)


def _version_only(before: str, after: str, rel: str) -> bool:
    """True when ``rel`` differs between the two texts only in its version."""
    if rel.endswith(".json"):
        try:
            old, new = json.loads(before), json.loads(after)
        except ValueError:
            return False
        if not (isinstance(old, dict) and isinstance(new, dict)):
            return False
        old.pop("version", None)
        new.pop("version", None)
        return bool(old == new)
    return INIT_VERSION.sub("", before) == INIT_VERSION.sub("", after)


def behaviour_changes(root: Path, base: str, head: str = "HEAD") -> list[str]:
    """Review M1: every path changed between ``base`` and ``head`` that counts as behaviour.

    Everything is behaviour except a path on the allow-list (`NON_BEHAVIOUR_PATHS`,
    `NON_BEHAVIOUR_FILES`) that is outside the hashed plugin content, and a version-only edit of
    a `VERSION_FILES` file. Renames count on both sides (``--no-renames``). Raises `ValueError`
    when git cannot diff."""
    diff = _git(root, "diff", "--name-only", "--no-renames", f"{base}..{head}")
    if diff.returncode != 0:
        raise ValueError(diff.stderr.strip())
    changed: list[str] = []
    for rel in diff.stdout.splitlines():
        if not _in_plugin(rel) and (
            rel in NON_BEHAVIOUR_FILES or rel.startswith(NON_BEHAVIOUR_PATHS)
        ):
            continue
        if rel in VERSION_FILES:
            old = _git(root, "show", f"{base}:{rel}")
            new = _git(root, "show", f"{head}:{rel}")
            if old.returncode == new.returncode == 0 and _version_only(old.stdout, new.stdout, rel):
                continue
        changed.append(rel)
    return changed


def marketplace_required(changelog: str) -> bool:
    """Ruling B1: the marketplace must track ``release/claude`` once a release is prepared, i.e.
    once the CHANGELOG has a dated ``X.Y.Z`` heading above the pre-1.0 milestone marker (the
    release-prep commit and everything after it). The rc freeze (``- Unreleased``) is not."""
    current = changelog.split(MILESTONE_MARKER, 1)[0]
    return any(RELEASE_VERSION.fullmatch(v) and _is_date(d or "") for v, d in _headings(current))


def marketplace_problems(root: Path, *, required: bool) -> list[str]:
    """Why ``.claude-plugin/marketplace.json`` is wrong: it must hold exactly one
    ``token-sparen`` entry whose source is the ``release/claude`` ref (``required``) or, before
    the first release only, ``./`` (the development head)."""
    where = MARKETPLACE.as_posix()
    try:
        data = json.loads((root / MARKETPLACE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{where} unreadable: {exc}"]
    plugins = data.get("plugins") if isinstance(data, dict) else None
    mine = [p for p in plugins or [] if isinstance(p, dict) and p.get("name") == PLUGIN_NAME]
    if len(mine) != 1:
        return [f"{where}: needs exactly one {PLUGIN_NAME!r} plugin entry"]
    source = mine[0].get("source")
    if source == release_source():
        return []
    if required:
        return [
            f"{where}: the {PLUGIN_NAME} source is {source!r}; a release must install the gated "
            f"tree {release_source()!r} (run scripts/bump_version.py --prepare-release)"
        ]
    if source != DEV_SOURCE:
        return [f"{where}: source {source!r} is neither {DEV_SOURCE!r} nor {release_source()!r}"]
    return []


def _dirty(root: Path) -> list[str]:
    """M1: tag mode checks what is committed, never local edits or stray files."""
    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        return [f"cannot read git status: {status.stderr.strip()}"]
    lines = status.stdout.splitlines()
    if not lines:
        return []
    shown = ", ".join(line[3:] for line in lines[:5]) + (" ..." if len(lines) > 5 else "")
    return [f"work tree not clean (tag mode checks only committed content): {shown}"]


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
            return problems + marketplace_problems(root, required=marketplace_required(changelog))
        problems += marketplace_problems(root, required=True)
        if version is not None:
            problems += release_branch_problems(root, version)
        if tag is None:
            return [*problems, "tag mode needs --tag vX.Y.Z"]
        problems += _dirty(root)
        if not is_release_tag(tag):
            problems.append(f"{tag!r} is not a release tag (only vX.Y.Z releases)")
        elif tag != f"v{version}":
            problems.append(f"tag {tag} does not match the version {version} (expected v{version})")
        release = RELEASE_VERSION.fullmatch(version)
        if release is None:
            return [*problems, f"version {version} is a development version; never released"]
        if not tree_ok:
            return problems
        if int(release.group(3)) == 0 or _measures(root, version):
            problems += _gate(root, tree, version)
        else:
            problems += _patch_rule(root, version, changelog, tree)
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
