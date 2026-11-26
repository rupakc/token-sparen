"""Synthetic tool outputs shared by mechanism and end-to-end tests."""

from __future__ import annotations

import json
import random
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


_WORDS = ["alpha", "beta", "gamma", "delta", "omega", "lorem", "ipsum", "kappa", "zeta"]


def python_source(lines: int = 1500) -> str:
    rng = random.Random(5)
    out = []
    for i in range(lines):
        if i % 12 == 0:
            out.append(f"def handler_{i}(request, payload):")
        else:
            out.append(f"    value_{i} = compute({rng.choice(_WORDS)!r}, {rng.randint(0, 999)})")
    return "\n".join(out)


def plain_text(lines: int = 900) -> str:
    rng = random.Random(3)
    return "\n".join(f"{i} " + " ".join(rng.choice(_WORDS) for _ in range(9)) for i in range(lines))


def big_diff(files: int = 12) -> str:
    out = []
    for f in range(files):
        out += [
            f"diff --git a/src/mod{f}.py b/src/mod{f}.py",
            "index 1111111..2222222 100644",
            f"--- a/src/mod{f}.py",
            f"+++ b/src/mod{f}.py",
            "@@ -1,80 +1,80 @@",
        ]
        for i in range(80):
            if i == 40:
                out += [f"-    old_value_{f} = {i}", f"+    new_value_{f} = {i + 1}"]
            else:
                out.append(f"     unchanged_line_{f}_{i} = compute_something({i})")
    return "\n".join(out)


def big_json() -> str:
    items = [
        {"id": i, "name": f"user-{i}", "email": f"user{i}@example.org", "roles": ["a", "b"]}
        for i in range(400)
    ]
    return json.dumps({"data": items, "total": 400}, indent=2)


def big_listing() -> str:
    out = []
    for d in range(60):
        out.append(f"./pkg/dir{d}:")
        out += [f"module_{d}_{i}.py" for i in range(20)]
        out.append("")
    return "\n".join(out)


def big_log() -> str:
    return "\n".join(
        f"2026-11-20T10:{i // 60 % 60:02d}:{i % 60:02d} INFO worker {i} processed batch ok"
        for i in range(1500)
    )
