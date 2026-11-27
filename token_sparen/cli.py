"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from token_sparen import __version__
from token_sparen.config import Config, load_config, state_home
from token_sparen.events import ToolCall
from token_sparen.store import jsonstore, keycache, paths, session

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # plugin or source checkout root
LAUNCHER_TEMPLATE = Path(str(resources.files("token_sparen").joinpath("assets", "statusline.sh")))
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
    ses.add_argument(
        "--session",
        help="session id (default: $CLAUDE_SESSION_ID, else this project's last active session)",
    )
    _project_opt(ses)
    conf = sub.add_parser("config", help="per-session config overrides (highest precedence)")
    conf_sub = conf.add_subparsers(dest="action", required=True)
    cs = conf_sub.add_parser("set", help="set <dotted.key> <value> for one session")
    cs.add_argument("key", help="e.g. guards.enabled or compression.min_tokens")
    cs.add_argument("value", help="true/false, a number or text")
    cs.add_argument(
        "--session",
        help="session id (default: $CLAUDE_SESSION_ID, else this project's last active session)",
    )
    _project_opt(cs)
    ext = sub.add_parser("extensions", help="list, trust or untrust extensions")
    ext_sub = ext.add_subparsers(dest="action", required=True)
    _project_opt(ext_sub.add_parser("list", help="drop-ins with trust status, and entry points"))
    tr = ext_sub.add_parser("trust", help="show a drop-in and trust its exact content (--yes)")
    tr.add_argument("path", type=Path)
    tr.add_argument("--yes", action="store_true", help="record trust (without it: preview only)")
    un = ext_sub.add_parser("untrust", help="revoke trust by drop-in path or sha256")
    un.add_argument("target")
    fold = sub.add_parser("fold", help="fold finished sessions into the project and global stores")
    fold.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")
    fold.add_argument("--all", action="store_true", help="every unfolded session of the project")
    fold.add_argument(
        "--older-than", type=float, default=0.0, help="--all: only sessions idle this many seconds"
    )
    fold.add_argument("--max", type=int, default=0, help="--all: at most this many (0 = no limit)")
    fold.add_argument(
        "--prune-apply",
        action="store_true",
        help="the SessionEnd fold: may write pending tool-pruning deny rules",
    )
    _project_opt(fold)
    rep = sub.add_parser(
        "replay", help="E1: replay transcripts offline; private aggregate report (--out)"
    )
    rep.add_argument("paths", nargs="+", type=Path, help="transcript files or directories")
    rep.add_argument(
        "--policy",
        default="static",
        help="default (production quality gate + static arm), static (ungated), engine (gated), "
        "a compression arm (ungated), or a comma-separated list (one pass)",
    )
    rep.add_argument("--split", choices=["dev", "test", "all"], default="all")
    rep.add_argument("--cutoff", help="YYYY-MM-DD: sessions before are dev, the rest test")
    rep.add_argument("--seed", type=int, default=0, help="engine policy: sampling seed")
    rep.add_argument("--out", type=Path, required=True, help="report file outside any repository")
    ope_p = sub.add_parser("ope", help="E3: off-policy evaluation of decision logs (--out)")
    ope_p.add_argument("--log", nargs="+", type=Path, required=True, help="decisions.jsonl files")
    ope_p.add_argument("--target", choices=["static", "engine-snapshot"], required=True)
    ope_p.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples")
    ope_p.add_argument("--seed", type=int, default=0)
    ope_p.add_argument(
        "--holdout-after",
        help="YYYY-MM-DD: evaluate only decisions logged on or after this date (UTC)",
    )
    ope_p.add_argument("--out", type=Path, required=True, help="report file outside any repository")
    facts = sub.add_parser("facts", help="learned repository facts: list, forget, disable")
    facts_sub = facts.add_subparsers(dest="action", required=True)
    _project_opt(facts_sub.add_parser("list", help="facts with status, evidence and posterior"))
    fg = facts_sub.add_parser("forget", help="delete a fact by its 12-hex id (never re-learned)")
    fg.add_argument("id")
    _project_opt(fg)
    _project_opt(facts_sub.add_parser("disable", help="stop injecting facts in this project"))
    _project_opt(facts_sub.add_parser("enable", help="undo `facts disable`"))
    pr = sub.add_parser("prune", help="opt-in learned tool pruning: status, enable, restore")
    pr_sub = pr.add_subparsers(dest="action", required=True)
    _project_opt(pr_sub.add_parser("status", help="state, pruned tools and candidates"))
    _project_opt(pr_sub.add_parser("enable", help="opt this project in (user action)"))
    _project_opt(pr_sub.add_parser("disable", help="opt out and restore every pruned tool"))
    rs = pr_sub.add_parser("restore", help="restore <tool> now, or --all (exact revert)")
    rs.add_argument("tool", nargs="?")
    rs.add_argument("--all", action="store_true", help="remove every deny rule we wrote")
    _project_opt(rs)
    return parser


_KEY_RE = re.compile(r"[0-9a-f]{16}")
_FOLD_TRIES = 5
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
    text = data.decode("utf-8", errors="surrogateescape")
    _record_recovery_read(args, text)
    _emit(text)
    return 0


def _record_recovery_read(args: argparse.Namespace, text: str) -> None:
    """Inside a session (resolved like `session disable`): a recovery read is full regret for the
    decision that saved this output, and its tokens go to the ledger. Silent when unresolved."""
    from token_sparen.estimate import estimate_tokens
    from token_sparen.learn import regret

    env = os.environ
    try:
        key = keycache.cached_project_key(_project_dir(args).resolve(), env)
        sid = _resolve_session(args, key, env)
    except (CliError, OSError, ValueError):
        return
    overlay = session.session_path(state_home(env), key, sid)
    regret.record_recovery_read(overlay, args.id, estimate_tokens(text), env)


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
    sid: str | None = getattr(args, "session", None) or os.environ.get("CLAUDE_SESSION_ID") or None
    if sid is None and required:
        raise CliError("no session id: pass --session ID or set CLAUDE_SESSION_ID")
    return sid


def _stats_data(args: argparse.Namespace) -> dict[str, Any]:
    from token_sparen.mechanisms import tracking

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
        overlays = sorted((pdir / "sessions").glob("*.json"))
    except OSError:
        overlays = []
    # the synthetic "cli" session (`run`) is not a real session, but its trims count
    sessions = sum(1 for f in overlays if f.stem != CLI_SESSION)
    gross = recovered = injected = 0
    for f in overlays:
        state = session.read_session(f)
        gross += tracking.ledger_savings(state)[0]
        recovered += _int(state.get("recovery_read_tokens"))
        injected += _int(state.get("injected_tokens"))
    sid = _session_id(args, required=False)
    ledger: dict[str, Any] | None = None
    if sid is not None:
        state = session.read_session(session.session_path(home, key, sid))
        ledger = {"id": sid, **{k: _int(state.get(k)) for k in _LEDGER_KEYS}}
        ledger["disabled"] = state.get("disabled") is True
        ledger["gross_saved"], ledger["net_saved"] = tracking.ledger_savings(state)
    return {
        "project_key": key,
        "sessions": sessions,
        "decisions": decisions,
        "tokens_before": before,  # decision log (current generation)
        "tokens_after": after,
        "saved": before - after,
        "gross_saved": gross,  # session ledgers, as the statusline counts them
        "net_saved": gross - recovered - injected,
        "recovery_read_tokens": recovered,
        "injected_tokens": injected,
        "session": ledger,
        "pruned_tools": _pruned_count(home, key, env),
    }


def _pruned_count(home: Path, key: str, env: Mapping[str, str]) -> int:
    from token_sparen.mechanisms import prune

    return prune.pruned_count(home, key, env)  # every checkout of the project


def _stats(args: argparse.Namespace) -> int:
    data = _stats_data(args)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"project {data['project_key']}: {data['sessions']} session(s)")
    print(
        f"saved net {data['net_saved']} (gross {data['gross_saved']} - recovery reads "
        f"{data['recovery_read_tokens']} - injected {data['injected_tokens']})"
    )
    print(
        f"decision log: tokens before {data['tokens_before']}, after {data['tokens_after']}, "
        f"saved {data['saved']}"
    )
    if data["pruned_tools"]:
        print(f"pruned tools: {data['pruned_tools']} (`token-sparen prune status`)")
    for point, arms in sorted(data["decisions"].items()):
        print(f"  {point}: " + ", ".join(f"{a}={n}" for a, n in sorted(arms.items())))
    ledger = data["session"]
    if ledger is not None:
        print(
            f"session {ledger['id']}: saved net {ledger['net_saved']} "
            f"(gross {ledger['gross_saved']})" + (" (disabled)" if ledger["disabled"] else "")
        )
        for k in _LEDGER_KEYS:
            print(f"  {k}: {ledger[k]}")
    return 0


LAST_ACTIVE_MAX_AGE_S = 6 * 3600


def _resolve_session(args: argparse.Namespace, key: str, env: Mapping[str, str]) -> str:
    """--session (non-empty) > $CLAUDE_SESSION_ID > this project's last active session (< 6 h)."""
    from token_sparen.mechanisms import tracking

    sid = _session_id(args, required=False)
    if sid is not None:
        return sid
    last, fresh = tracking.last_active(state_home(env), key, LAST_ACTIVE_MAX_AGE_S)
    if last is None:
        raise CliError(
            "no session id: pass --session ID or set CLAUDE_SESSION_ID "
            "(no recently active session recorded for this project)"
        )
    if not fresh:
        raise CliError(
            f"no session id: the last active session {last} is more than 6 h old; "
            "pass --session ID (inside Claude Code: /token-sparen disable)"
        )
    return last


def _session_cmd(args: argparse.Namespace) -> int:
    env = os.environ
    key = keycache.cached_project_key(_project_dir(args).resolve(), env)
    sid = _resolve_session(args, key, env)
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


def _config_set(args: argparse.Namespace) -> int:
    from token_sparen.config import SESSION_KEY, packaged_defaults, parse_value

    parts = str(args.key).split(".")
    node: object = packaged_defaults()
    for part in parts:
        node = node.get(part) if isinstance(node, dict) and part else None
    if node is None or isinstance(node, dict) or len(parts) < 2:
        raise CliError(f"unknown config key {args.key!r} (see params/defaults.toml)")
    env = os.environ
    key = keycache.cached_project_key(_project_dir(args).resolve(), env)
    sid = _resolve_session(args, key, env)
    value = parse_value(args.value)

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        over = state.get(SESSION_KEY)
        tree: dict[str, Any] = over if isinstance(over, dict) else {}
        cur = tree
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value
        state[SESSION_KEY] = tree
        return state

    session.update_session(session.session_path(state_home(env), key, sid), apply)
    print(f"token-sparen: {args.key} = {value!r} for session {sid}")
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


MIN_TOKENS_FLOOR = 2000


def _below_floor(cfg: Config) -> list[str]:
    """Configured ``*.min_tokens`` values under the 2000-token floor (ignored at runtime)."""
    out = []
    for key in ("compression.min_tokens", "read_dedupe.min_tokens"):
        value = cfg.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            if value < MIN_TOKENS_FLOOR:
                out.append(f"{key} = {value:g} is below the floor; {MIN_TOKENS_FLOOR} is used")
    return out


def _host_platform() -> str:
    return sys.platform


def python3_missing_hint(platform: str, which: Callable[[str], str | None] | None = None) -> str:
    """Why ``python3`` matters and, on Windows, what to use instead for the CLI."""
    which = shutil.which if which is None else which
    text = "python3 not found on PATH; hooks run python3"
    if platform != "win32":
        return text
    if which("py") is not None:
        other = "py -3"
    elif which("python") is not None:
        other = "python"
    else:
        return f"{text}. Install Python >= 3.11 (python.org or the Microsoft Store) with PATH set"
    return (
        f"{text}. `{other}` exists: run the CLI as `{other} -m token_sparen`, and make `python3` "
        "resolve (Microsoft Store Python provides it) so the hooks can start"
    )


def _round_trip(root: Path) -> tuple[bool, str]:
    py = shutil.which("python3")
    if py is None:
        return False, python3_missing_hint(_host_platform())
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
            # a pipeline check: the static arm always compresses, a cold learner may not
            TOKEN_SPAREN_LEARN__ENABLED="false",
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
        add(_WARN, "python3 on PATH", python3_missing_hint(_host_platform()))
    elif path_py < (3, 11):
        add(_WARN, "python3 on PATH", f"{path_py[0]}.{path_py[1]} < 3.11; hooks use python3")
    floors = _below_floor(load_config(Path.cwd(), env))
    if floors:
        add(_WARN, "min_tokens", "; ".join(floors))
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
            print(
                "token-sparen: launcher (token_sparen/assets/statusline.sh) not found",
                file=sys.stderr,
            )
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
    for group, name, value, api in eps:
        text = extensions.printable(f"{name} = {value}")
        print(f"{'entry-point':<24} {group:<31} {text}  [{api}]")
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


def _fold_one(
    sid: str, key: str, home: Path, params: Mapping[str, Any], prune_apply: bool = False
) -> Any:
    from token_sparen.learn import overlay

    for attempt in range(_FOLD_TRIES):
        try:
            return overlay.fold(sid, key, home, params=params, prune_apply=prune_apply)
        except jsonstore.StoreBusy:
            if attempt == _FOLD_TRIES - 1:
                raise CliError(f"fold {sid}: state is busy, try again later", 1) from None
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


def _fold_cmd(args: argparse.Namespace) -> int:
    from token_sparen.adapters.claude import settings  # noqa: F401 - registers the deny writer
    from token_sparen.learn import overlay

    env = os.environ
    home = state_home(env)
    project = _project_dir(args).resolve()
    key = keycache.cached_project_key(project, env)
    params = load_config(project, env).data
    if args.all:
        limit = args.max if args.max > 0 else 1_000_000
        age = max(0.0, args.older_than)
        indexed = overlay.indexed_sessions(key, home, older_than=age, limit=limit)
        scanned = overlay.pending_sessions(key, home, older_than=age, limit=limit, max_scan=None)
        sids = list(dict.fromkeys([*indexed, *scanned]))[:limit]  # index first, scan fallback
        reports = [_fold_one(s, key, home, params) for s in sids]
        done = [r for r in reports if r.staged]
        cells = sum(r.cells for r in done)
        tripped = sorted({p for r in done for p in r.tripped})
        line = f"token-sparen fold: {len(done)} session{'' if len(done) == 1 else 's'} folded"
        line += f", {cells} cells merged"
        if tripped:
            line += f"; breaker tripped: {', '.join(tripped)}"
        print(line)
        return 0
    sid = args.session or env.get("CLAUDE_SESSION_ID")
    if not sid:
        raise CliError("fold needs --session ID (or $CLAUDE_SESSION_ID) or --all")
    print(_fold_one(sid, key, home, params, prune_apply=args.prune_apply).line())
    return 0


def _private_out(path: Path) -> Path:
    from token_sparen.replay import report

    try:
        return report.check_out_path(path)
    except report.OutPathError as exc:
        raise CliError(str(exc)) from None


def _replay_cmd(args: argparse.Namespace) -> int:
    """E1. Prints aggregates only: no paths, ids or transcript text (errors included)."""
    from datetime import date

    from token_sparen.replay import replay, report

    out = _private_out(args.out)
    try:
        cutoff = date.fromisoformat(args.cutoff) if args.cutoff else replay.DEFAULT_CUTOFF
    except ValueError:
        raise CliError("--cutoff must be YYYY-MM-DD") from None
    try:
        replay.resolve_policies(args.policy, load_config(Path.cwd(), os.environ))
    except ValueError as exc:
        raise CliError(str(exc)) from None
    try:
        result = replay.run(args.paths, args.policy, args.split, cutoff, seed=args.seed)
        report.write_private(out, result.to_dict())
    except Exception as exc:  # noqa: BLE001 - never echo a message that could quote a path
        raise CliError(f"replay failed ({type(exc).__name__})", 1) from None
    for line in replay.summary_lines(result):
        print(line)
    print("token-sparen replay: private report written (mode 0600)")
    return 0


def _ope_target(
    args: argparse.Namespace, logs: list[Path]
) -> tuple[Any, list[Any], dict[str, float]]:
    import random

    from token_sparen.learn import ope
    from token_sparen.learn.engine import Engine
    from token_sparen.learn.hierarchy import StoreChain
    from token_sparen.learn.policy import StaticPolicy

    if args.target == "static":
        log, regrets = ope.load_log(logs)
        return ope.StaticTarget(StaticPolicy({})), log, regrets
    cfg = load_config(Path.cwd(), os.environ)
    home = state_home(os.environ)
    # Every file is read once and outcomes join globally by decision id (a decision and its
    # outcome may sit in different rotation generations); decisions are then grouped by the
    # project their file belongs to, one engine per project.
    by_id: dict[str, Any] = {}
    engines: dict[str, Any] = {}
    for path in logs:
        key = path.resolve().parent.name
        if not _KEY_RE.fullmatch(key):
            raise CliError("engine-snapshot needs logs at <state>/projects/<key>/decisions.jsonl")
        if key not in engines:
            engine = Engine(StoreChain(key, home, None, params=cfg.data), cfg, random.Random(0))
            engines[key] = ope.EngineTarget(engine)
        for rec in ope.load_log([path])[0]:
            by_id[rec.decision_id] = engines[key]
    log_all, regrets_all = ope.load_log(logs)

    class _ByProject:
        def probs(self, rec: Any) -> Any:
            return by_id[rec.decision_id].probs(rec)

    return _ByProject(), log_all, regrets_all


def _ope_cmd(args: argparse.Namespace) -> int:
    """E3. Prints aggregates only."""
    from datetime import UTC, datetime

    from token_sparen.learn import ope
    from token_sparen.replay import report

    out = _private_out(args.out)
    since: float | None = None
    if args.holdout_after:
        try:
            day = datetime.strptime(args.holdout_after, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise CliError("--holdout-after must be YYYY-MM-DD") from None
        since = day.timestamp()
    try:
        target, log, regrets = _ope_target(args, list(args.log))
        if since is not None:
            log = [r for r in log if isinstance(r.ts, int | float) and r.ts >= since]
        points = ope.evaluate(
            log,
            target,
            regrets=regrets,
            n_boot=max(1, args.n_boot),
            seed=args.seed,
            tau=_tau_config(),
            **_ope_thresholds(),
        )
        report.write_private(
            out,
            {
                "version": 1,
                "kind": "e3-off-policy-evaluation",
                "target": args.target,
                "n_boot": max(1, args.n_boot),
                "clip": ope.CLIP,
                "holdout_after": args.holdout_after,
                "notes": ope.NOTES,
                "points": points,
            },
        )
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - never echo a message that could quote a path
        raise CliError(f"ope failed ({type(exc).__name__})", 1) from None
    for name, agg in points.items():
        print(
            f"{name}: n {agg['n']} (no context {agg['no_context']}), "
            f"unsupported mass {agg['unsupported_mass']:.3f}, "
            f"clipped {agg['clipped_fraction']:.3f}, "
            f"regret known {agg['regret_known_fraction']:.3f}"
        )
        for part in ("savings", "regret", "combined"):
            for est in ("ips", "snips", "dr"):
                lo, hi = agg[part][est]["ci95"]
                print(f"  {part} {est} {agg[part][est]['value']:.4f} [{lo:.4f}, {hi:.4f}]")
        for variant in ("optimistic", "pessimistic"):
            for est in ("ips", "dr"):
                got = agg["regret_per_trim"][variant][est]
                if got is not None:
                    lo, hi = got["ci95"]
                    print(
                        f"  regret per trim ({variant}) {est} {got['value']:.4f} "
                        f"[{lo:.4f}, {hi:.4f}]"
                    )
        upper = agg["constraint_upper"]
        bound = "" if upper is None else f", pessimistic upper {upper:.4f}"
        print(
            f"  constraint {agg['constraint_status']}{bound} vs tau {agg['tau']:.4f} "
            f"(outcome coverage {agg['outcome_coverage']:.3f}, unsupported trim mass "
            f"{agg['unsupported_trim_fraction']:.3f})"
        )
    print("token-sparen ope: private report written (mode 0600)")
    return 0


def _tau_config() -> dict[str, float]:
    """``[learn.tau]`` of the merged config (numbers in [0, 1] only): the constraint and the
    combined objective use the user's regret targets."""
    raw = load_config(Path.cwd(), os.environ).get("learn.tau")
    out: dict[str, float] = {}
    for tier, value in raw.items() if isinstance(raw, dict) else ():
        if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1:
            out[str(tier)] = float(value)
    return out


def _ope_thresholds() -> dict[str, Any]:
    """``[ope]`` thresholds of the merged config (user-only keys; finite numbers only)."""
    cfg = load_config(Path.cwd(), os.environ)
    out: dict[str, Any] = {}
    for key in ("max_unsupported_trim", "min_outcome_coverage", "min_trims"):
        value = cfg.get(f"ope.{key}")
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            out[key] = int(value) if key == "min_trims" else float(value)
    return out


def _facts_pdir(args: argparse.Namespace) -> Path:
    key = keycache.cached_project_key(_project_dir(args).resolve(), os.environ)
    return paths.project_dir_for(state_home(os.environ), key)


def _facts_cmd(args: argparse.Namespace) -> int:
    from token_sparen.facts import lifecycle, miners
    from token_sparen.facts.sanitize import fact_id, render

    pdir = _facts_pdir(args)
    store = pdir / miners.FACTS_FILE
    if args.action == "list":
        doc = miners.load(pdir, os.environ)
        if doc.get("disabled") is True:
            print("fact injection disabled for this project (`token-sparen facts enable`)")
        table = doc.get("facts")
        rows = []
        for fid, raw in table.items() if isinstance(table, dict) else ():
            fact = miners.parse_entry(raw)
            if fact is None or fact_id(fact) != fid or not isinstance(raw, dict):
                continue  # tampered or malformed: never printed
            post = lifecycle.effect(raw)
            est = lifecycle.estimated_effect(raw)
            extra = "" if est is None else f", effect {est:+.2f} vs withheld"
            rows.append(
                f"{fid}  {fact.kind:<13} {lifecycle.status(raw):<9} "
                f"evidence {len(fact.evidence_sessions)}, mean {post.mean:.2f}{extra}\n"
                f"    {render(fact)}"
            )
        if not rows:
            print("no repository facts learned for this project yet")
        for row in sorted(rows):
            print(row)
        return 0
    if args.action == "forget":
        if _ID_RE.fullmatch(args.id) is None:
            raise CliError("facts forget needs a 12-hex fact id (see `token-sparen facts list`)")
        found = [False]

        def forget(doc: dict[str, Any]) -> dict[str, Any]:
            table = doc.get("facts")
            if isinstance(table, dict) and args.id in table:
                del table[args.id]
                found[0] = True
            raw = doc.get("forgotten")
            gone = [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []
            if found[0] and args.id not in gone:
                doc["forgotten"] = [*gone, args.id][-miners.MAX_FACTS :]
            return doc

        if not store.is_file():
            raise CliError(f"no fact {args.id}", code=1)
        jsonstore.update(store, forget, {}, os.environ)
        if not found[0]:
            raise CliError(f"no fact {args.id}", code=1)
        print(f"token-sparen facts: forgot {args.id} (it will not be learned again)")
        return 0
    off = args.action == "disable"

    def toggle(doc: dict[str, Any]) -> dict[str, Any]:
        if not doc:
            doc = {"version": miners.VERSION, "facts": {}}
        if off:
            doc["disabled"] = True
        else:
            doc.pop("disabled", None)
        return doc

    pdir.mkdir(parents=True, exist_ok=True)
    jsonstore.update(store, toggle, {}, os.environ)
    state = "disabled" if off else "enabled"
    print(f"token-sparen facts: injection {state} for this project")
    return 0


def _prune_project(args: argparse.Namespace) -> Path:
    """``--project``, else ``CLAUDE_PROJECT_DIR``, else the git top level of the cwd (a
    subdirectory resolves to its checkout), else the cwd."""
    if args.project:
        return Path(args.project).resolve()
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    return Path.cwd().resolve()


def _prune_cmd(args: argparse.Namespace) -> int:
    from token_sparen.adapters.claude import settings  # registers the deny-rule writer
    from token_sparen.mechanisms import prune

    env = os.environ
    home = state_home(env)
    project = _prune_project(args)
    key = keycache.cached_project_key(project, env)
    target = settings.local_settings_path(project)
    s = prune.settings_for(load_config(project, env).data, env)
    if args.action == "status":
        doc = prune.read_doc(home, key, env, checkout=project)
        print("\n".join(prune.status_lines(doc, s, target, env)))
        return 0
    if args.action == "enable":
        seeded = prune.enable(home, key, project, env)
        print(
            "token-sparen prune: enabled for this project (opt-in). MCP servers available but "
            f"never used across enough sessions will be denied in {target} (backed up once to "
            f"{target.name}{settings.BACKUP_SUFFIX}), only while the project is idle; savings "
            "are modest (their tool names and server instructions). Undo with "
            "`token-sparen prune restore <tool>`, `restore --all` or `prune disable`."
        )
        if seeded is not None:
            print(
                f"found {seeded} earlier sessions in the last {prune.HISTORY_DAYS} days; tools "
                "used there are never pruned."
            )
            if seeded == 0:
                print(
                    "warning: no earlier Claude Code transcripts were found for this project; "
                    "pruning waits until a session has been recorded (its folder is read then)."
                )
        if not s.allow:
            print("note: this project's token-sparen.toml sets [prune] allow = false")
        exposure = settings.git_exposure(project)
        if exposure is not None:
            why = {
                "tracked": "it is tracked by git",
                "unignored": "it is not covered by .gitignore",
            }.get(exposure, "git cannot confirm it is untracked and ignored")
            print(f"warning: nothing will be written to {target} while {why}.")
        return 0
    if args.action == "disable":
        done = prune.disable(home, key, env, checkout=project)
        print(
            "token-sparen prune: disabled for this project"
            + (f"; restored {', '.join(done)}" if done else "")
        )
        if s.enabled:
            print("note: [prune] enabled = true in your config.toml still enables it here")
        return 0
    if args.all == (args.tool is not None):
        raise CliError("prune restore needs a tool name or --all")
    doc = prune.read_doc(home, key, env, checkout=project)
    if args.all:
        every = prune.restore(home, key, None, None, env, checkout=project)  # every checkout
        print(f"token-sparen prune: restored {', '.join(every)}" if every else "nothing is pruned")
        return 0
    where = prune.holders(doc, args.tool)  # a tool of a pruned server maps to the server
    here = prune.file_key(target)
    if not where:
        raise CliError(f"{args.tool} is not pruned by token-sparen in this project", code=1)
    restored: dict[str, list[str]]
    if here in where:
        restored = {here: prune.restore(home, key, project, where[here], env)}
    else:  # pruned in another checkout of the project (sibling clone, worktree)
        wanted = sorted({r for rs in where.values() for r in rs})
        restored = prune.restore_everywhere(home, key, wanted, env)
    restored = {f: rs for f, rs in restored.items() if rs}
    if not restored:
        raise CliError(f"could not change {', '.join(where)}; see `token-sparen doctor`", code=1)
    for fkey, rules in restored.items():
        print(f"token-sparen prune: restored {', '.join(rules)} in {fkey}")
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "statusline":
        return _statusline(args)
    handlers = {
        "show": _show,
        "run": _run_cmd,
        "stats": _stats,
        "doctor": _doctor,
        "session": _session_cmd,
        "extensions": _extensions,
        "config": _config_set,
        "fold": _fold_cmd,
        "replay": _replay_cmd,
        "ope": _ope_cmd,
        "facts": _facts_cmd,
        "prune": _prune_cmd,
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
