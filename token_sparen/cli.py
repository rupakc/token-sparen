"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen import __version__
from token_sparen.config import load_config, state_home
from token_sparen.events import ToolCall
from token_sparen.store import jsonstore, keycache, paths, session

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # plugin or source checkout root
LAUNCHER_TEMPLATE = PACKAGE_ROOT / "scripts" / "statusline.sh"
_FALLBACK_LINE = "FALLBACK_ROOT=''"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-sparen",
        description="Learned, recoverable token optimisation for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"token-sparen {__version__}")
    sub = parser.add_subparsers(dest="command")
    sl = sub.add_parser("statusline", help="live Claude Code statusline (opt-in settings write)")
    sl_sub = sl.add_subparsers(dest="action", required=True)
    for name, text in (
        ("install", "add the statusline to Claude Code settings (backed up, composes)"),
        ("uninstall", "restore the original statusLine setting"),
    ):
        p = sl_sub.add_parser(name, help=text)
        p.add_argument("--settings", type=Path, help="settings.json (default: Claude Code's)")
    sl_sub.add_parser("render", help="render one line from the statusline JSON on stdin")
    show = sub.add_parser("show", help="print a saved full output by its 12-hex id")
    show.add_argument("id")
    _project_opt(show)
    run = sub.add_parser("run", help="run a command and compress its output: run -- <cmd...>")
    run.add_argument("cmd", nargs="*")
    _project_opt(run)
    stats = sub.add_parser("stats", help="session ledger and project totals")
    stats.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")
    stats.add_argument("--json", action="store_true", help="machine-readable output")
    _project_opt(stats)
    doctor = sub.add_parser("doctor", help="check the installation end to end")
    doctor.add_argument("--fix", action="store_true", help="remove leftover temp files")
    ses = sub.add_parser("session", help="per-session kill switch")
    ses.add_argument("action", choices=["disable", "enable"])
    ses.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")
    _project_opt(ses)
    ext = sub.add_parser("extensions", help="list, trust or untrust extensions")
    ext_sub = ext.add_subparsers(dest="action", required=True)
    _project_opt(ext_sub.add_parser("list", help="drop-ins with trust status, and entry points"))
    tr = ext_sub.add_parser("trust", help="show a drop-in and trust its exact content (--yes)")
    tr.add_argument("path", type=Path)
    tr.add_argument("--yes", action="store_true", help="record trust (without it: preview only)")
    un = ext_sub.add_parser("untrust", help="revoke trust by drop-in path or sha256")
    un.add_argument("target")
    for name in _STUBS:
        stub = sub.add_parser(name, help="available after M2/M3")
        stub.add_argument("rest", nargs="*")
    return parser


_STUBS = ("fold", "replay", "facts", "prune")
_ID_RE = re.compile(r"[0-9a-f]{12}")
_SHELL_SYNTAX = re.compile(r"[\s|&;<>()$`\\*?{}~!#]")


class CliError(Exception):
    """A user-facing failure: printed as ``token-sparen: <message>``, exit code ``code``."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _project_opt(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project", type=Path, help="project directory (default: current directory)")


def _project_dir(args: argparse.Namespace) -> Path:
    return Path(args.project) if args.project else Path.cwd()


def _emit(text: str) -> None:
    """Write text to stdout byte-exact as UTF-8 (surrogateescape), bypassing newline translation."""
    sys.stdout.flush()  # keep order with anything the text layer already wrote
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    buffer.write(text.encode("utf-8", errors="surrogateescape"))
    buffer.flush()


def _show(args: argparse.Namespace) -> int:
    if not _ID_RE.fullmatch(args.id):
        raise CliError(f"invalid id {args.id!r}: expected 12 lowercase hex characters")
    path = _project_dir(args) / ".token-sparen" / "outputs" / f"{args.id}.txt"
    try:
        data = path.read_bytes()
    except OSError:
        print(f"token-sparen: no saved output {args.id} in {path.parent}", file=sys.stderr)
        return 1
    # Task 21 hook: count this read as a recovery read (ledger recovery_read_tokens) here.
    _emit(data.decode("utf-8", errors="surrogateescape"))
    return 0


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _cli_session_disabled(project: Path, env: Mapping[str, str]) -> bool:
    sid = env.get("CLAUDE_SESSION_ID") or CLI_SESSION
    key = keycache.cached_project_key(project, env)
    return (
        session.read_session(session.session_path(state_home(env), key, sid)).get("disabled")
        is True
    )


def _run_cmd(args: argparse.Namespace) -> int:
    from token_sparen.mechanisms.compression import compress_text

    cmd: list[str] = list(args.cmd)
    if not cmd:
        raise CliError("run: no command given (usage: token-sparen run -- <command...>)")
    if len(cmd) == 1 and _SHELL_SYNTAX.search(cmd[0]) and not os.path.exists(cmd[0]):
        text_cmd = cmd[0]
        shell = sys.platform == "win32"
        argv: str | list[str] = text_cmd if shell else ["/bin/sh", "-c", text_cmd]
    else:
        text_cmd, shell, argv = shlex.join(cmd), False, cmd
    try:
        proc = subprocess.run(  # noqa: S603 - the user's own command
            argv,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        print(f"token-sparen: command not found: {cmd[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"token-sparen: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        return 126
    text = proc.stdout.decode("utf-8", errors="surrogateescape")
    project = _project_dir(args).resolve()
    env = os.environ
    shown = text
    cfg = load_config(project, env)
    if not cfg.disabled and not _cli_session_disabled(project, env):
        tool = ToolCall(name="Bash", category="shell", input={"command": text_cmd}, use_id=None)
        try:
            shown, _info = compress_text(text, tool, cfg, project, env)
        except Exception as exc:  # noqa: BLE001 - never lose the command's output
            from token_sparen.config import log_error

            log_error(env, "cli.run", f"{type(exc).__name__}: {exc}")
            shown = text
    _emit(shown if shown == text or shown.endswith("\n") else shown + "\n")
    return _exit_code(proc.returncode)


def _read_decisions(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


CLI_SESSION = "cli"
_LEDGER_KEYS = ("trims", "tokens_removed", "injected_tokens", "recovery_read_tokens")


def _session_id(args: argparse.Namespace, *, required: bool) -> str | None:
    sid: str | None = args.session or os.environ.get("CLAUDE_SESSION_ID") or None
    if sid is None and required:
        raise CliError("no session id: pass --session ID or set CLAUDE_SESSION_ID")
    return sid


def _stats_data(args: argparse.Namespace) -> dict[str, Any]:
    env = os.environ
    home = state_home(env)
    key = keycache.cached_project_key(_project_dir(args).resolve(), env)
    pdir = paths.project_dir_for(home, key)
    decisions: dict[str, dict[str, int]] = {}
    before = after = 0
    for rec in _read_decisions(pdir / "decisions.jsonl"):
        point, arm = rec.get("point"), rec.get("arm")
        if not isinstance(point, str) or not isinstance(arm, str):
            continue
        arms = decisions.setdefault(point, {})
        arms[arm] = arms.get(arm, 0) + 1
        before += _int(rec.get("tokens_before"))
        after += _int(rec.get("tokens_after"))
    try:
        sessions = sum(  # the synthetic "cli" session (`run`) is not a real session
            1 for f in (pdir / "sessions").glob("*.json") if f.stem != CLI_SESSION
        )
    except OSError:
        sessions = 0
    sid = _session_id(args, required=False)
    ledger: dict[str, Any] | None = None
    if sid is not None:
        state = session.read_session(session.session_path(home, key, sid))
        ledger = {"id": sid, **{k: _int(state.get(k)) for k in _LEDGER_KEYS}}
        ledger["disabled"] = state.get("disabled") is True
    return {
        "project_key": key,
        "sessions": sessions,
        "decisions": decisions,
        "tokens_before": before,
        "tokens_after": after,
        "saved": before - after,
        "session": ledger,
    }


def _stats(args: argparse.Namespace) -> int:
    data = _stats_data(args)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"project {data['project_key']}: {data['sessions']} session(s)")
    print(
        f"tokens before {data['tokens_before']}, after {data['tokens_after']}, "
        f"saved {data['saved']}"
    )
    for point, arms in sorted(data["decisions"].items()):
        print(f"  {point}: " + ", ".join(f"{a}={n}" for a, n in sorted(arms.items())))
    ledger = data["session"]
    if ledger is not None:
        print(f"session {ledger['id']}" + (" (disabled)" if ledger["disabled"] else ""))
        for k in _LEDGER_KEYS:
            print(f"  {k}: {ledger[k]}")
    return 0


def _session_cmd(args: argparse.Namespace) -> int:
    sid = _session_id(args, required=True)
    assert sid is not None
    env = os.environ
    key = keycache.cached_project_key(_project_dir(args).resolve(), env)
    overlay = session.session_path(state_home(env), key, sid)
    off = args.action == "disable"

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        if off:
            state["disabled"] = True
        else:
            state.pop("disabled", None)
        return state

    session.update_session(overlay, apply)
    print(f"token-sparen: {'disabled' if off else 'enabled'} for session {sid}")
    return 0


# --- doctor -----------------------------------------------------------------------------------

_OK, _BAD, _WARN = "✓", "✗", "!"


def _fake_pytest_payload(project: Path) -> str:
    lines = ["=== test session starts ===", "collected 3000 items", ""]
    lines += [
        f"tests/test_mod_{i // 40:02d}.py::test_case_{i:04d} PASSED   [{min(99, i // 30):3d}%]"
        for i in range(3000)
    ]
    lines += ["FAILED tests/test_core.py::test_it - AssertionError", "1 failed, 2999 passed"]
    return json.dumps(
        {
            "session_id": "doctor",
            "cwd": str(project),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -v"},
            "tool_use_id": "doctor-1",
            "tool_response": {
                "stdout": "\n".join(lines),
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
    )


def _plugin_root(env: Mapping[str, str]) -> Path:
    raw = env.get("CLAUDE_PLUGIN_ROOT")
    return Path(raw) if raw else PACKAGE_ROOT


def _check_hooks_json(root: Path) -> tuple[bool, str]:
    hooks_json = root / "hooks" / "hooks.json"
    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
        groups = data["hooks"]
        commands = [h["command"] for g in groups.values() for e in g for h in e["hooks"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return False, f"{hooks_json} invalid ({type(exc).__name__}: {exc})"
    if not commands or not all("${CLAUDE_PLUGIN_ROOT}" in c for c in commands):
        return False, "hook commands must reference ${CLAUDE_PLUGIN_ROOT}"
    if not (root / "hooks" / "run.py").is_file():
        return False, f"{root / 'hooks' / 'run.py'} missing"
    return True, f"{root} ({len(commands)} hook commands)"


def _round_trip(root: Path) -> tuple[bool, str]:
    py = shutil.which("python3")
    if py is None:
        return False, "python3 not found on PATH (hooks run python3)"
    with tempfile.TemporaryDirectory(prefix="token-sparen-doctor-") as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))
        }
        env.update(
            TOKEN_SPAREN_HOME=str(Path(tmp) / "home"),
            CLAUDE_PROJECT_DIR=str(project),
            TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="5000",
        )
        try:
            proc = subprocess.run(  # noqa: S603
                [py, "-I", str(root / "hooks" / "run.py"), "PostToolUse"],
                input=_fake_pytest_payload(project),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not run the hook ({type(exc).__name__}: {exc})"
    if "updatedToolOutput" in proc.stdout and "[token-sparen: kept " in proc.stdout:
        return True, "synthetic PostToolUse compressed and carried the marker"
    return False, f"hook output had no updatedToolOutput/marker: {proc.stdout[:120]!r}"


def _python3_version() -> tuple[int, int] | None:
    py = shutil.which("python3")
    if py is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603
            [py, "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout.split()
        return int(out[0]), int(out[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _doctor(args: argparse.Namespace) -> int:
    env = os.environ
    results: list[tuple[str, str, str]] = []

    def add(mark: str, name: str, detail: str) -> None:
        results.append((mark, name, detail))

    ver = sys.version_info
    ok = ver >= (3, 11)
    add(_OK if ok else _BAD, "python", f"{ver.major}.{ver.minor}.{ver.micro} (need >= 3.11)")
    path_py = _python3_version()
    if path_py is None:
        add(_WARN, "python3 on PATH", "not found; hooks run python3")
    elif path_py < (3, 11):
        add(_WARN, "python3 on PATH", f"{path_py[0]}.{path_py[1]} < 3.11; hooks use python3")
    home = state_home(env)
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / f".doctor-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(_OK, "state home", f"{home} is writable")
    except OSError as exc:
        add(_BAD, "state home", f"{home} not writable ({exc})")
    root = _plugin_root(env)
    if "CLAUDE_PLUGIN_ROOT" not in env and not (root / "hooks" / "hooks.json").is_file():
        add(
            _OK,
            "plugin",
            "skipped (pip-only install; run inside Claude Code plugin to check hooks)",
        )
        good = False
    else:
        good, detail = _check_hooks_json(root)
        add(_OK if good else _BAD, "plugin", detail)
    if good:
        good, detail = _round_trip(root)
        add(_OK if good else _BAD, "hook round-trip", detail)
    if sys.platform == "win32":
        add(_OK, "statusline", "launcher not supported on Windows yet (skipped)")
    try:
        leftovers = [p for p in home.rglob("*.tmp-*") if p.is_file()]
    except OSError:
        leftovers = []
    if leftovers and args.fix:
        for p in leftovers:
            with contextlib.suppress(OSError):
                p.unlink()
        add(_OK, "temp files", f"removed {len(leftovers)} leftover *.tmp-* file(s)")
    elif leftovers:
        add(_BAD, "temp files", f"{len(leftovers)} leftover *.tmp-* file(s); run doctor --fix")
    else:
        add(_OK, "temp files", "no leftovers")
    for mark, name, detail in results:
        print(f"{mark} {name}: {detail}")
    return 1 if any(m == _BAD for m, _, _ in results) else 0


def _launcher_path() -> Path:
    return state_home(os.environ) / "statusline.sh"


def _write_launcher() -> Path:
    """Copy the launcher to a stable path (plugin updates move the plugin directory)."""
    text = LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace(_FALLBACK_LINE, f"FALLBACK_ROOT={shlex.quote(str(PACKAGE_ROOT))}", 1)
    dest = _launcher_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o755)
    jsonstore.replace_with_retry(tmp, dest)
    return dest


def _statusline(args: argparse.Namespace) -> int:
    from token_sparen.adapters.claude import settings

    target: Path = args.settings or settings.settings_path(os.environ)
    if args.action == "install":
        if not LAUNCHER_TEMPLATE.is_file():
            print("token-sparen: launcher scripts/statusline.sh not found", file=sys.stderr)
            return 1
        try:
            settings.load_settings(target)  # refuse before writing anything (the launcher too)
        except ValueError as exc:
            print(f"token-sparen: {exc}", file=sys.stderr)
            return 1
        try:
            launcher = _write_launcher()
        except OSError as exc:
            print(f"token-sparen: cannot write the launcher ({exc})", file=sys.stderr)
            return 1
        result = settings.install_statusline(target, str(launcher))
    else:
        result = settings.uninstall_statusline(target, launcher=str(_launcher_path()))
        if result.action in {"removed", "restored"} and not settings.has_installs(os.environ):
            with contextlib.suppress(OSError):
                _launcher_path().unlink()
    print(result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


def _extensions_list(args: argparse.Namespace) -> int:
    from token_sparen import extensions

    home = state_home(os.environ)
    dropins = extensions.discover(_project_dir(args), home, os.environ)
    eps = extensions.entry_point_listing()
    if not dropins and not eps:
        print("no extensions found (drop-ins: <project>/.token-sparen/extensions/*.py,")
        print(f"  {extensions.printable(str(home / 'extensions'))}/*.py; entry points: none)")
        return 0
    for d in dropins:
        sha = d.sha256[:12] if d.sha256 else "-"
        print(f"{d.status:<24} {sha:<12} {extensions.printable(str(d.path))}")
    for group, name, value in eps:
        text = extensions.printable(f"{name} = {value}")
        print(f"{'entry-point':<24} {group:<31} {text}")
    return 0


def _extensions_trust(args: argparse.Namespace) -> int:
    from token_sparen import extensions

    path: Path = args.path
    shown = extensions.printable(str(path))
    try:
        sha, lines = extensions.preview(path)
    except extensions.UnsafeFile as exc:
        raise CliError(f"{shown}: refusing to trust ({exc.reason})") from None
    print(f"sha256 {sha}  {shown}")
    print(f"--- first {len(lines)} lines ---")
    for line in lines:
        print(line)
    print("---")
    if not args.yes:
        print("not trusted: re-run with --yes to trust exactly this content", file=sys.stderr)
        return 1
    try:
        extensions.trust(path, state_home(os.environ), os.environ, expect_sha=sha)
    except extensions.UnsafeFile as exc:
        raise CliError(f"{shown}: refusing to trust ({exc.reason})") from None
    print(f"trusted {sha} (any edit to the file revokes trust)")
    return 0


def _extensions_untrust(args: argparse.Namespace) -> int:
    from token_sparen import extensions

    removed = extensions.untrust(args.target, state_home(os.environ), os.environ)
    print(f"removed {removed} trusted entr{'y' if removed == 1 else 'ies'}")
    return 0 if removed else 1


def _extensions(args: argparse.Namespace) -> int:
    handlers = {
        "list": _extensions_list,
        "trust": _extensions_trust,
        "untrust": _extensions_untrust,
    }
    return handlers[args.action](args)


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "statusline":
        return _statusline(args)
    if args.command in _STUBS:
        print(f"token-sparen {args.command}: available after M2/M3")
        return 0
    handlers = {
        "show": _show,
        "run": _run_cmd,
        "stats": _stats,
        "doctor": _doctor,
        "session": _session_cmd,
        "extensions": _extensions,
    }
    return handlers[args.command](args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:2] == ["statusline", "render"]:  # passthrough: --compose -- <command>
        from token_sparen.adapters.claude import statusline

        return statusline.main(argv[2:])
    tail: list[str] = []
    if argv[:1] == ["run"] and "--" in argv:  # everything after -- belongs to the command
        cut = argv.index("--")
        argv, tail = argv[:cut], argv[cut + 1 :]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for --version/--help/errors
        return exc.code if isinstance(exc.code, int) else 1
    if args.command is None:
        parser.print_usage()
        return 0
    if args.command == "run":
        args.cmd = [*args.cmd, *tail]
    try:
        return _dispatch(args)
    except CliError as exc:
        print(f"token-sparen: {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - never show a traceback
        print(f"token-sparen: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
