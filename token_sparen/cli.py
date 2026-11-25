"""Command-line entry point."""

from __future__ import annotations

import argparse

from token_sparen import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-sparen",
        description="Learned, recoverable token optimisation for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"token-sparen {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for --version/--help/errors
        return exc.code if isinstance(exc.code, int) else 1
    parser.print_usage()
    return 0
