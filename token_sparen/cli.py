"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import sys
from pathlib import Path

from token_sparen import __version__
from token_sparen.config import state_home
from token_sparen.store import jsonstore

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
    return parser


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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:2] == ["statusline", "render"]:  # passthrough: --compose -- <command>
        from token_sparen.adapters.claude import statusline

        return statusline.main(argv[2:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for --version/--help/errors
        return exc.code if isinstance(exc.code, int) else 1
    if args.command == "statusline":
        return _statusline(args)
    parser.print_usage()
    return 0
