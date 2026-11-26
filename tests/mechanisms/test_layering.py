"""Layering: mechanisms are host-neutral and never import a host adapter."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import token_sparen

PKG = Path(token_sparen.__file__).resolve().parent
_HOST_NEUTRAL = ("mechanisms", "compress", "learn", "store")


def _imports(source: str, package: str) -> list[str]:
    """Absolute names imported by ``source``, a module of ``package`` (relative ones resolved)."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                mod = ".".join([*base, mod] if mod else base)
            found.append(mod)
            found += [f"{mod}.{a.name}" for a in node.names]
    return found


def _offends(name: str) -> bool:
    return name == "token_sparen.adapters" or name.startswith("token_sparen.adapters.")


@pytest.mark.parametrize(
    "source",
    [
        "import token_sparen.adapters.claude\n",
        "from token_sparen.adapters.claude.translate import render_tool_output\n",
        "from token_sparen import adapters\n",
        "from ..adapters import base\n",
        "from ..adapters.claude import translate\n",
        "from .. import adapters\n",
    ],
)
def test_detector_catches_absolute_and_relative_imports(source: str) -> None:
    assert any(_offends(n) for n in _imports(source, "token_sparen.mechanisms"))


def test_detector_resolves_deeper_relative_imports() -> None:
    names = _imports("from ...adapters import base\n", "token_sparen.compress.sub")
    assert any(_offends(n) for n in names)
    assert not any(_offends(n) for n in _imports("from . import base\n", "token_sparen.compress"))


def test_host_neutral_packages_never_import_adapters() -> None:
    offenders = []
    for sub in _HOST_NEUTRAL:
        for path in sorted((PKG / sub).rglob("*.py")):
            rel = path.relative_to(PKG.parent).with_suffix("")
            parts = list(rel.parts)
            package = ".".join(parts if parts[-1] == "__init__" else parts[:-1])
            package = package.removesuffix(".__init__")
            for name in _imports(path.read_text(encoding="utf-8"), package):
                if _offends(name):
                    offenders.append(f"{path.relative_to(PKG).as_posix()}: {name}")
    assert offenders == []


def test_host_adapter_protocol_has_output_rendering() -> None:
    from token_sparen.adapters.base import HostAdapter
    from token_sparen.adapters.claude.translate import ClaudeAdapter

    for name in ("render_tool_output", "replaceable"):
        assert name in HostAdapter.__dict__
        assert callable(getattr(ClaudeAdapter(), name))
