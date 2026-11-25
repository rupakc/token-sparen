"""Synthetic tool outputs shared by mechanism and end-to-end tests."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def big_pytest(target_bytes: int = 50_000) -> str:
    """A verbose pytest run of roughly ``target_bytes`` with two real failures at the end."""
    lines = [
        "============================= test session starts ==============================",
        "platform linux -- Python 3.12.1, pytest-8.1.0, pluggy-1.4.0",
        "rootdir: /srv/demo",
        "collected 900 items",
        "",
    ]
    tail = (FIXTURES / "reports" / "pytest_fail.txt").read_text(encoding="utf-8")
    size = sum(len(x) + 1 for x in lines) + len(tail)
    i = 0
    while size < target_bytes:
        pct = min(99, i // 9)
        ln = f"tests/test_mod_{i // 40:02d}.py::test_case_{i:04d} PASSED{' ' * 20}[{pct:3d}%]"
        lines.append(ln)
        size += len(ln) + 1
        i += 1
    return "\n".join(lines) + "\n" + tail
