"""Generate on-disk state fixtures from every pre-1.0 milestone tag (dev-time, run once).

For each tag (``v0.1.0-m1`` ... ``v0.4.0-m4``) the tag's tree is exported (``git archive``) to a
temporary directory and its own hooks and CLI are driven on synthetic input with an isolated
``TOKEN_SPAREN_HOME``, project directory and Claude config directory. The resulting state trees
are copied to ``tests/fixtures/state/<tag>/`` with every absolute path rewritten to a
placeholder (``<project>``, ``<home>``, ``<claude>``, ``<transcripts>``, ``<plugin>``); lock
files are dropped. A ``legacy`` set adds hand-written shapes that intermediate 0.x commits
wrote and no tag captured (v1 posteriors, the v1 prune store with its ``files`` table, a flat
``applied`` list, a corrupt document).

The project is a git repository with a fixed synthetic remote, so its project key is the same
on every machine: ``tests/store/test_upgrade.py`` recreates it and replays the fixtures on the
current code.

Usage: ``python scripts/make_state_fixtures.py [--tags v0.1.0-m1 ...] [--out DIR]``
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from sampleoutputs import big_pytest  # noqa: E402

TAGS = ("v0.1.0-m1", "v0.2.0-m2", "v0.3.0-m3", "v0.4.0-m4")
REMOTE = "https://example.invalid/fixtures/demo.git"
OUT = ROOT / "tests" / "fixtures" / "state"
SESSIONS = ("fixture-s1", "fixture-s2")
OPEN_SESSION = "fixture-s3"
DROPIN = (
    '"""Synthetic drop-in extension for the state fixtures (provides nothing)."""\n\n'
    'TOKEN_SPAREN_EXTENSION = {"api": "1.x", "compressors": []}\n'
)
SKIP_SUFFIXES = (".lock",)
# the recovery folder's ``*`` .gitignore would hide the fixture from git (it is recreated)
SKIP_NAMES = (".gitignore",)
DETACHED_FOLD_S = 3.0


def export(tag: str, dest: Path) -> None:
    """The tag's tree, without registering a worktree."""
    blob = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", tag],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest, filter="data")


def init_project(project: Path) -> None:
    project.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", REMOTE]):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
    (project / "big.py").write_text(
        "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(700)), encoding="utf-8"
    )


def write_transcript(path: Path, session: str, project: Path) -> None:
    """A small synthetic transcript: two prompts, a test run, a read."""
    recs: list[dict[str, Any]] = [
        {"type": "user", "sessionId": session, "cwd": str(project),
         "message": {"role": "user", "content": "why does test_cache_eviction fail?"}},
        {"type": "assistant", "isSidechain": False, "message": {
            "id": f"msg-{session}-1", "model": "model-a",
            "content": [{"type": "tool_use", "id": f"tu-{session}-1", "name": "Bash",
                         "input": {"command": "pytest -v"}}],
            "usage": {"input_tokens": 9000, "output_tokens": 40,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu-{session}-1", "content": "1 failed"}]}},
        {"type": "assistant", "isSidechain": False, "message": {
            "id": f"msg-{session}-2", "model": "model-a",
            "content": [{"type": "tool_use", "id": f"tu-{session}-2", "name": "Read",
                         "input": {"file_path": str(project / "big.py")}}],
            "usage": {"input_tokens": 12000, "output_tokens": 30,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        {"type": "user", "message": {"role": "user", "content": "thanks, now run the tests again"}},
    ]  # fmt: skip
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def hook(tree: Path, event: str, payload: dict[str, Any], env: dict[str, str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(tree / "hooks" / "run.py"), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{event} failed ({proc.returncode}): {proc.stderr[-400:]}")


def cli(tree: Path, args: Sequence[str], env: dict[str, str], cwd: Path) -> None:
    """Best effort: a subcommand an older tag lacks simply fails."""
    subprocess.run(
        [sys.executable, "-I", str(tree / "hooks" / "run_cli.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )


def drive(tree: Path, work: Path) -> None:
    home, project, claude, scripts = (work / n for n in ("home", "proj", "claude", "transcripts"))
    init_project(project)
    claude.mkdir()
    scripts.mkdir()
    home.mkdir(mode=0o700)
    (claude / "settings.json").write_text('{"model": "default"}\n', encoding="utf-8")
    (home / "config.toml").write_text(
        # min_tokens below the 2000 floor: the floor warning record (floor_warned.json)
        "[adapter]\nlatency_budget_ms = 5000\n[compression]\nmin_tokens = 500\n",
        encoding="utf-8",
    )
    ext = home / "extensions"
    ext.mkdir(mode=0o700)
    (ext / "fixture_ext.py").write_text(DROPIN, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    env.update(
        TOKEN_SPAREN_HOME=str(home),
        CLAUDE_PROJECT_DIR=str(project),
        CLAUDE_PLUGIN_ROOT=str(tree),
        CLAUDE_CONFIG_DIR=str(claude),
        TOKEN_SPAREN_TEST_SEED="2",
    )
    cli(tree, ["prune", "enable", "--project", str(project)], env, project)
    cli(tree, ["statusline", "install"], env, project)
    cli(tree, ["extensions", "trust", str(ext / "fixture_ext.py"), "--yes"], env, project)
    text = big_pytest(12_000)
    big = (project / "big.py").read_text(encoding="utf-8")
    for n, sid in enumerate(SESSIONS):
        transcript = scripts / f"{sid}.jsonl"
        write_transcript(transcript, sid, project)
        base = {"session_id": sid, "cwd": str(project), "transcript_path": str(transcript)}

        def ev(name: str, base: dict[str, Any] = base, **extra: Any) -> None:
            hook(tree, name, {**base, "hook_event_name": name, **extra}, env)

        ev("SessionStart", source="startup", model="model-a")
        ev("UserPromptSubmit", prompt="why does test_cache_eviction fail?")
        for i in range(2):
            ev(
                "PostToolUse",
                tool_name="Bash",
                tool_input={"command": "pytest -v"},
                tool_use_id=f"tu-{sid}-bash-{i}",
                tool_response={"stdout": text, "stderr": "", "interrupted": False,
                               "isImage": False},
            )  # fmt: skip
            read_input = {"file_path": str(project / "big.py")}
            ev("PreToolUse", tool_name="Read", tool_input=read_input,
               tool_use_id=f"tu-{sid}-read-{i}")  # fmt: skip
            ev(
                "PostToolUse",
                tool_name="Read",
                tool_input=read_input,
                tool_use_id=f"tu-{sid}-read-{i}",
                tool_response={"type": "text", "file": {"filePath": read_input["file_path"],
                               "content": big, "numLines": big.count("\n"), "startLine": 1,
                               "totalLines": big.count("\n")}},
            )  # fmt: skip
        for i in range(3):
            ev(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_input={"command": "make build"},
                tool_use_id=f"tu-{sid}-fail-{i}",
                error="Exit code 2\nerror: missing target build",
            )
        ev("PreCompact", trigger="auto")
        ev("SessionEnd", reason="other")
        time.sleep(DETACHED_FOLD_S)  # SessionEnd spawns the session's fold detached
        if n == 0:
            cli(tree, ["session", "disable", "--session", sid], env, project)
            cli(tree, ["session", "enable", "--session", sid], env, project)
            cli(tree, ["config", "set", "guards.enabled", "true", "--session", sid], env, project)
            cli(tree, ["facts", "disable"], env, project)
            cli(tree, ["facts", "enable"], env, project)
    cli(tree, ["fold"], env, project)
    # a session left with its turn open (loop-guard turn state in the overlay, unfolded)
    sid = OPEN_SESSION
    transcript = scripts / f"{sid}.jsonl"
    write_transcript(transcript, sid, project)
    base = {"session_id": sid, "cwd": str(project), "transcript_path": str(transcript)}
    for name, extra in (
        ("SessionStart", {"source": "startup", "model": "model-a"}),
        ("UserPromptSubmit", {"prompt": "build it"}),
    ):
        hook(tree, name, {**base, "hook_event_name": name, **extra}, env)
    for i in range(3):
        hook(
            tree,
            "PostToolUseFailure",
            {
                **base,
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_input": {"command": "make build"},
                "tool_use_id": f"tu-{sid}-fail-{i}",
                "error": "Exit code 2\nerror: missing target build",
            },
            env,
        )


def placeholders(work: Path, tree: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for base, name in (
        (work / "proj", "<project>"),
        (work / "home", "<home>"),
        (work / "claude", "<claude>"),
        (work / "transcripts", "<transcripts>"),
        (tree, "<plugin>"),
        (work, "<tmp>"),
    ):
        for spelling in {str(base), os.path.realpath(base)}:
            pairs.append((spelling, name))
    return sorted(pairs, key=lambda p: -len(p[0]))  # longest first: nested dirs before parents


def rewrite(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
        text = text.replace(json.dumps(old)[1:-1], new)
    return text


def copy_tree(src: Path, dest: Path, pairs: list[tuple[str, str]]) -> int:
    count = 0
    if not src.is_dir():
        return 0
    for path in sorted(src.rglob("*")):
        if (
            not path.is_file()
            or path.name.endswith(SKIP_SUFFIXES)
            or path.name in SKIP_NAMES
            or ".tmp-" in path.name
        ):
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        try:
            out = rewrite(data.decode("utf-8"), pairs).encode("utf-8")
        except UnicodeDecodeError:
            out = data
        target.write_bytes(out)
        count += 1
    return count


def legacy(dest: Path) -> None:
    """Shapes written by intermediate 0.x commits that no milestone tag captured."""
    key = hashlib.sha256(b"example.invalid/fixtures/demo").hexdigest()[:16]  # REMOTE's key
    pdir = dest / "home" / "projects" / key
    pdir.mkdir(parents=True)
    (dest / "home" / "global").mkdir(parents=True)
    docs: dict[Path, Any] = {
        # M2 early: v1 posteriors held Beta posteriors per arm (now read as empty, rebuilt)
        pdir / "posteriors.json": {
            "version": 1,
            "compression": {"bash|test|large": {"light": {"a": 3.0, "b": 1.0}}},
        },
        dest / "home" / "global" / "posteriors.json": {
            "version": 2,
            "points": {},
            "applied": ["fold-old-1", "fold-old-2"],  # legacy flat list (now per project)
        },
        # M3 early: v1 prune store kept the per-checkout records in a "files" table
        pdir / "prune.json": {
            "version": 1,
            "enabled": True,
            "tools": {"NotebookEdit": {"sessions": 3, "used": 0}},
            "files": {"settings-a": {"owned": ["NotebookEdit"], "pending_add": []}},
        },
        pdir / "facts.json": {"version": 1, "facts": {}, "disabled": True},
        pdir / "breaker.json": {"tripped": {}, "applied": ["fold-old-1"]},
    }
    for path, doc in docs.items():
        path.write_text(json.dumps(doc), encoding="utf-8")
    (pdir / "pretrim.json").write_text('{"bash|test": [1, 2', encoding="utf-8")  # truncated
    (pdir / "sessions").mkdir()
    (pdir / "sessions" / "legacy-s1.json").write_text(
        json.dumps({"tokens_saved": 120, "trims": 1, "folded_at": 1}), encoding="utf-8"
    )
    (pdir / "decisions.jsonl").write_text(
        '{"ts": 1, "point": "compression", "arm": "light"}\nnot json\n', encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tags", nargs="*", default=list(TAGS))
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    for tag in args.tags:
        with tempfile.TemporaryDirectory(prefix="ts-fixture-") as tmp:
            work = Path(tmp)
            tree = work / "tree"
            export(tag, tree)
            drive(tree, work)
            dest = args.out / tag
            shutil.rmtree(dest, ignore_errors=True)
            pairs = placeholders(work, tree)
            n = copy_tree(work / "home", dest / "home", pairs)
            n += copy_tree(work / "proj" / ".token-sparen", dest / "project", pairs)
            n += copy_tree(work / "claude", dest / "claude", pairs)
            print(f"{tag}: {n} files -> {dest.relative_to(ROOT)}")
    dest = args.out / "legacy"
    shutil.rmtree(dest, ignore_errors=True)
    legacy(dest)
    print(f"legacy -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
