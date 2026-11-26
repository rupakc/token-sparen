"""Layering: mechanisms are host-neutral and never import a host adapter."""

from __future__ import annotations

import ast
from pathlib import Path

import token_sparen

PKG = Path(token_sparen.__file__).resolve().parent
_HOST_NEUTRAL = ("mechanisms", "compress", "learn", "store")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            found.append(mod)
            found += [f"{mod}.{a.name}" for a in node.names]
    return found


def test_host_neutral_packages_never_import_adapters() -> None:
    offenders = []
    for sub in _HOST_NEUTRAL:
        for path in sorted((PKG / sub).rglob("*.py")):
            for name in _imports(path):
                if name.startswith("token_sparen.adapters") or name == "token_sparen.adapters":
                    offenders.append(f"{path.relative_to(PKG).as_posix()}: {name}")
    assert offenders == []


def test_host_adapter_protocol_has_output_rendering() -> None:
    from token_sparen.adapters.base import HostAdapter
    from token_sparen.adapters.claude.translate import ClaudeAdapter

    for name in ("render_tool_output", "replaceable"):
        assert name in HostAdapter.__dict__
        assert callable(getattr(ClaudeAdapter(), name))
