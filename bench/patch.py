"""The graded patch of one run (M5 Task 6; Review Focus 2).

`extract_patch` stages everything the run left in the task work tree, then unstages the tool
artefacts of ``excludes`` (`bench.arms.Arm.patch_excludes`) **only** where the path was
untracked at ``base_commit``: a file the agent created is kept, a file an arm's tool created
(``.token-sparen/``, ``CLAUDE.md``, ``RTK.md``, …) is dropped, and an edit to a file the
repository already tracked is always the agent's (a tool appending to a tracked ``CLAUDE.md`` is
graded with the agent's patch, by design). The patch is ``git diff --cached --binary
--full-index <base>``, so commits the agent made anyway are included. Exclude entries are
relative to the repository root: ``dir/`` matches everything below that directory, any other
entry one file. New files in paths the work tree's ``.gitignore`` ignores are not in the patch
(as in SWE-bench practice).

Hostile repositories (fix round 1, I2): the agent can write ``/testbed/.git/config`` and
``.gitattributes``. Extraction therefore never uses the repository's own git dir: it stages into
a fresh temporary git dir (clean config, no hooks, no templates) whose object store borrows the
repository's objects read-only through ``objects/info/alternates``. No repo-local
``core.fsmonitor``, ``core.hooksPath``, filter or textconv driver, ``diff.external``, prefix or
colour setting is ever read, so none runs or changes the output; attributes naming a driver find
no driver. Every call also carries explicit clean ``-c`` settings and diff flags, git runs with
``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_CONFIG_GLOBAL=/dev/null``, and the agent's repository (its
index included) is never modified. The repository's ``.git/info/exclude`` is not consulted.

Stdlib only and runnable as a script: `entry.sh` runs it inside the task container with the
toolchain Python under its own time limit (``python -I patch.py --repo /testbed --base <commit>
--excludes-env TSB_PATCH_EXCLUDES --out /run-out/patch.diff``); the output file is written to a
temporary name and renamed over ``--out`` (a planted link at ``--out`` is replaced, never
followed). The fake backend calls `extract_patch` directly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence

Run = Callable[[list[str]], str]
# environment the git calls inherit (an explicit allow-list: no token, no GIT_* redirects)
_GIT_ENV_KEYS = ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")
_BATCH = 200
GIT_TIMEOUT_S = 50.0  # per git call; `entry.sh` bounds the whole extraction
CLEAN_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.splitIndex=false",
    "-c",
    "core.quotePath=true",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.bare=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.external=",
    "-c",
    "diff.renames=false",
    "-c",
    "color.ui=false",
    "--literal-pathspecs",
)
DIFF_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class PatchError(RuntimeError):
    """A git command failed (or overran) while extracting the patch."""


def git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The git environment: allow-listed keys of ``environ``, no system or global config."""
    env = {k: environ[k] for k in _GIT_ENV_KEYS if k in environ}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_LITERAL_PATHSPECS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git_runner(environ: Mapping[str, str] | None = None, *, timeout: float = GIT_TIMEOUT_S) -> Run:
    """A `Run` that executes git with `git_env` and returns stdout (surrogate-escaped)."""
    env = git_env(os.environ if environ is None else environ)

    def run(argv: list[str]) -> str:
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                env=env,
                check=False,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise PatchError(f"{' '.join(argv[:3])}… timed out after {timeout} s") from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise PatchError(f"git failed: {err[:500]}")
        return proc.stdout.decode("utf-8", "surrogateescape")

    return run


def excluded(path: str, excludes: Sequence[str]) -> bool:
    """``path`` (repo-relative POSIX) is a tool artefact named by ``excludes``."""
    for entry in excludes:
        if entry.endswith("/") and path.startswith(entry):
            return True
        if not entry.endswith("/") and path == entry:
            return True
    return False


def extract_patch(repo: str, base_commit: str, excludes: Sequence[str], *, run: Run) -> str:
    """The run's patch against ``base_commit`` without tool artefacts (module docstring)."""
    objects = os.path.join(os.path.abspath(repo), ".git", "objects")
    if not os.path.isdir(objects) or os.path.islink(os.path.join(repo, ".git")):
        raise PatchError("the task repository has no plain .git directory")
    with tempfile.TemporaryDirectory(prefix="tsb-patch-") as scratch:
        gitdir = os.path.join(scratch, "git")
        run(["git", *CLEAN_CONFIG, "init", "-q", "--bare", "--template=", gitdir])
        alternates = os.path.join(gitdir, "objects", "info", "alternates")
        os.makedirs(os.path.dirname(alternates), exist_ok=True)
        with open(alternates, "w", encoding="utf-8") as fh:
            fh.write(objects + "\n")
        git = ["git", *CLEAN_CONFIG, f"--git-dir={gitdir}", f"--work-tree={os.path.abspath(repo)}"]
        run([*git, "read-tree", base_commit])
        run([*git, "add", "-A"])
        added = run(
            [*git, "diff", "--cached", *DIFF_FLAGS, "--name-only", "--diff-filter=A", "-z"]
            + [base_commit]
        )
        drop = [p for p in added.split("\0") if p and excluded(p, excludes)]
        for i in range(0, len(drop), _BATCH):
            run([*git, "update-index", "--force-remove", "--", *drop[i : i + _BATCH]])
        return run([*git, "diff", "--cached", *DIFF_FLAGS, "--binary", "--full-index", base_commit])


def _write_atomic(out: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(out))
    fd, tmp = tempfile.mkstemp(prefix=".patch-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)  # replaces a planted link itself, never follows it
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Extract the graded patch of one run.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--excludes-env", help="variable holding newline-separated excludes")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    excludes = list(args.exclude)
    if args.excludes_env:
        excludes += [e for e in env.get(args.excludes_env, "").splitlines() if e]
    try:
        patch = extract_patch(args.repo, args.base, excludes, run=git_runner(env))
        _write_atomic(args.out, patch.encode("utf-8", "surrogateescape"))
    except (PatchError, OSError) as exc:
        print(f"patch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
