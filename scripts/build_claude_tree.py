"""Build the `release/claude` plugin tree: what users install from the marketplace and directory.

The tree holds the hashed plugin content (`scripts/plugin_hash.py` ``PLUGIN_PATHS``:
``.claude-plugin/plugin.json``, ``hooks/``, ``skills/``, ``token_sparen/``; committed files
only, written byte for byte from the git blobs of the release commit), plus ``LICENSE``,
``NOTICE``, ``SECURITY.md`` and the README with its Evidence block replaced by a link to the
site's Evidence page (so writing the Evidence never changes the plugin hash). Nothing else
ships: no ``marketplace.json`` (ruling N1: distribution metadata, unhashed;
users add the marketplace from ``main``), no CHANGELOG, tests, evals, bench, docs, scripts,
``.claude/``, ``.github/``, ``.gitattributes`` or caches.

The built tree must meet the plugin directory limits (`tree_problems`); `build_claude_tree`
raises `TreeError` otherwise and leaves no partial tree behind.

Usage: ``python scripts/build_claude_tree.py --out DIR [--root .]`` prints the file count and
the plugin hash of the built tree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.freeze import FreezeError, _read_blobs  # noqa: E402  (git cat-file --batch)
from scripts.plugin_hash import PLUGIN_PATHS, plugin_hash  # noqa: E402

PLUGIN_SOURCES: tuple[str, ...] = tuple(PLUGIN_PATHS)
EXTRA_FILES = ("LICENSE", "NOTICE", "SECURITY.md")
README = "README.md"
SITE_URL = "https://rupakc.github.io/token-sparen/"
EVIDENCE_URL = SITE_URL + "benchmarks-methodology.html#evidence"
EVIDENCE_LINK = (
    f"Benchmark results (with their 95 % confidence intervals, the measured version and plugin "
    f"hash) are published on the [Evidence page]({EVIDENCE_URL})."
)
# both marker spellings in use (docs/user pages and the README renderer)
EVIDENCE_BLOCK = re.compile(
    r"<!--\s*evidence(?::start)?\s*-->.*?<!--\s*(?:/evidence|evidence:end)\s*-->", re.S
)
MAX_FILES = 512
MAX_FILE_BYTES = 256 * 1024
MIN_README_WORDS = 40
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"})
NEVER_SHIPPED_NAMES = frozenset({".gitattributes", ".gitignore", ".gitmodules", ".DS_Store"})
NEVER_SHIPPED_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache", ".mypy_cache"})
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
UNPINNED_LAUNCHERS = frozenset({"npx", "uvx", "bunx", "pnpx"})


class TreeError(ValueError):
    """The plugin tree cannot be built or breaks a directory limit."""


def _git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise TreeError(f"git {' '.join(args)} failed in {root}: {proc.stderr.strip()}")
    return proc.stdout


def _shipped(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in NEVER_SHIPPED_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    return name not in NEVER_SHIPPED_NAMES and not name.endswith((".pyc", ".pyo"))


def _tree_entries(root: Path, ref: str, paths: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """``(mode, blob sha, path)`` of every committed file under ``paths`` at ``ref``."""
    out = _git(
        root, "ls-tree", "-r", "-z", "--full-tree", ref, "--", *(p.rstrip("/") for p in paths)
    )
    entries: list[tuple[str, str, str]] = []
    for record in filter(None, out.split("\0")):
        meta, rel = record.split("\t", 1)
        mode, kind, sha = meta.split(" ")
        if mode == "120000":
            raise TreeError(f"symlink in plugin tree: {rel}")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise TreeError(f"not a regular file in plugin tree: {rel} (mode {mode})")
        if rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
            raise TreeError(f"unsafe path in plugin tree: {rel!r}")
        entries.append((mode, sha, rel))
    return sorted(entries, key=lambda e: e[2])


def tracked_plugin_files(root: Path, ref: str = "HEAD") -> list[tuple[str, bool]]:
    """``(path, executable)`` for every committed shipped file under the plugin sources."""
    return [
        (rel, mode == "100755")
        for mode, _, rel in _tree_entries(root, ref, PLUGIN_SOURCES)
        if _shipped(rel)
    ]


def shipped_readme(text: str) -> str:
    """The README as shipped: the Evidence block becomes a link to the site's Evidence page."""
    return EVIDENCE_BLOCK.sub(lambda _: EVIDENCE_LINK, text)


def _blobs(root: Path, shas: list[str]) -> list[bytes]:
    try:
        return _read_blobs(root, shas)
    except (FreezeError, ValueError) as exc:
        raise TreeError(f"cannot read git blobs: {exc}") from exc


def build_claude_tree(root: Path, out: Path, *, ref: str = "HEAD") -> list[Path]:
    """Write the plugin tree of commit ``ref`` of ``root`` into the new directory ``out``.

    Files are written byte for byte from the git blobs at ``ref`` (never from the work tree),
    so the tree and its plugin hash do not depend on the OS or on git's checkout settings
    (``core.autocrlf``, eol attributes) and uncommitted edits never ship. Returns the files.
    Raises `FileExistsError` when ``out`` exists and `TreeError` when a source is missing, a
    symlink, or the built tree breaks a limit (`tree_problems`); ``out`` is then removed.
    """
    root = root.resolve()
    if out.exists() or out.is_symlink():
        raise FileExistsError(f"{out} already exists; the tree is built into a new directory")
    if not ref or ref.startswith("-"):
        raise TreeError(f"bad ref {ref!r}")
    commit = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    plugin = [e for e in _tree_entries(root, commit, PLUGIN_SOURCES) if _shipped(e[2])]
    extras = _tree_entries(root, commit, (*EXTRA_FILES, README))
    found = {rel for _, _, rel in extras}
    missing = [rel for rel in (*EXTRA_FILES, README) if rel not in found]
    if missing:
        raise TreeError(f"not committed at {ref}: {', '.join(missing)}")
    entries = plugin + extras
    out.mkdir(parents=True)
    try:
        written: list[Path] = []
        for (mode, _, rel), data in zip(
            entries, _blobs(root, [e[1] for e in entries]), strict=True
        ):
            if rel == README:
                data = shipped_readme(data.decode("utf-8")).encode("utf-8")
            target = out.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if mode == "100755" else 0o644)
            written.append(target)
        problems = tree_problems(out)
        if problems:
            raise TreeError("plugin tree breaks the directory limits: " + "; ".join(problems))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return sorted(written)


# --- checks -------------------------------------------------------------------------------------


def _walk(tree: Path) -> Iterator[tuple[str, Path]]:
    for dirpath, dirnames, filenames in os.walk(tree):
        here = Path(dirpath)
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = here / name
            yield path.relative_to(tree).as_posix(), path


def name_problems(rel: str) -> list[str]:
    """Reasons ``rel`` is not a portable file name on Windows and macOS."""
    problems: list[str] = []
    for part in rel.split("/"):
        stem = part.split(".", 1)[0].upper()
        if WINDOWS_BAD_CHARS.search(part):
            problems.append(f"{rel}: character not allowed on Windows")
        if stem in WINDOWS_RESERVED:
            problems.append(f"{rel}: reserved device name on Windows")
        if part.endswith((".", " ")):
            problems.append(f"{rel}: trailing dot or space not allowed on Windows")
        if unicodedata.normalize("NFC", part) != part:
            problems.append(f"{rel}: name is not NFC-normalised (macOS)")
        if len(part.encode("utf-8")) > 255:
            problems.append(f"{rel}: name longer than 255 bytes")
    return problems


def case_clashes(rels: list[str]) -> list[str]:
    """Paths that collide on a case-insensitive (default macOS/Windows) file system."""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for rel in rels:
        folded = unicodedata.normalize("NFC", rel).casefold()
        if folded in seen:
            clashes.append(f"{rel}: clashes with {seen[folded]} on a case-insensitive disk")
        seen[folded] = rel
    return clashes


def _hook_commands(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from _hook_commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from _hook_commands(item)


def command_problems(command: str, tree: Path) -> list[str]:
    """A hook command may reference files only via ``${CLAUDE_PLUGIN_ROOT}`` (inside the tree)."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [f"hook command {command!r}: cannot parse ({exc})"]
    problems: list[str] = []
    if not argv:
        return [f"hook command {command!r}: empty"]
    if any(arg in UNPINNED_LAUNCHERS for arg in argv):
        problems.append(f"hook command {command!r}: unpinned launcher (npx/uvx)")
    referenced = False
    for arg in argv:
        if arg.startswith(PLUGIN_ROOT_VAR + "/"):
            referenced = True
            rel = arg[len(PLUGIN_ROOT_VAR) + 1 :]
            target = tree / rel
            if ".." in rel.split("/") or not target.is_file() or target.is_symlink():
                problems.append(f"hook command {command!r}: {rel} is not a file in the tree")
        elif "/" in arg or "\\" in arg or "$" in arg:
            problems.append(
                f"hook command {command!r}: {arg!r} is not referenced via {PLUGIN_ROOT_VAR}"
            )
    if not referenced:
        problems.append(f"hook command {command!r}: no file referenced via {PLUGIN_ROOT_VAR}")
    return problems


def readme_words(text: str) -> int:
    """Words in ``text`` outside fenced code, inline code and HTML comments."""
    text = re.sub(r"^(```|~~~).*?^\1[^\n]*$", " ", text, flags=re.S | re.M)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


def tree_problems(tree: Path) -> list[str]:
    """Every directory-limit problem of the built tree (empty = ok)."""
    problems: list[str] = []
    files: list[str] = []
    names: list[str] = []
    for rel, path in _walk(tree):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            problems.append(f"{rel}: symlink")
            continue
        problems += name_problems(rel)
        names.append(rel)
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            problems.append(f"{rel}: not a regular file")
            continue
        files.append(rel)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES and path.suffix.lower() not in IMAGE_SUFFIXES:
            problems.append(f"{rel}: {size} bytes, over the 256 KiB limit for non-image files")
    problems += case_clashes(names)
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files, over the {MAX_FILES} files limit")
    hooks = tree / "hooks" / "hooks.json"
    manifest = tree / ".claude-plugin" / "plugin.json"
    for config in (hooks, manifest):
        if config.is_file() and not config.is_symlink():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                problems.append(f"{config.relative_to(tree).as_posix()}: invalid JSON ({exc})")
                continue
            for command in _hook_commands(data):
                problems += command_problems(command, tree)
    readme = tree / README
    if not readme.is_file():
        problems.append(f"{README} missing")
    else:
        words = readme_words(readme.read_text(encoding="utf-8"))
        if words < MIN_README_WORDS:
            problems.append(f"{README}: {words} words outside code, needs >= {MIN_README_WORDS}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_claude_tree", description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="new directory for the tree")
    parser.add_argument("--root", default=ROOT, type=Path, help="repository root (default: repo)")
    args = parser.parse_args(argv)
    try:
        written = build_claude_tree(args.root, args.out)
    except (OSError, TreeError) as exc:
        print(f"build_claude_tree: {exc}", file=sys.stderr)
        return 1
    print(f"ok {len(written)} files in {args.out}; plugin hash {plugin_hash(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
