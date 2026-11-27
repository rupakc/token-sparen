"""Documentation guards."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Digits allowed in the method document: ISO dates, section numbers, milestone/eval ids and the
# literal method parameters (95 % interval, 100 % E2 gate, 0.2 tau margin, 8 groups, 30 trims).
_ALLOWED = [
    r"\d{4}-\d{2}-\d{2}",
    r"§\d+(?:\.\d+)*",
    r"\b[EM]\d\b",
    r"evals/e2/run\.py",
    r"\b0\.2\b",
    r"\b95 %",
    r"\b100 %",
    r"\b8 project groups",
    r"\b30 trims",
]


def test_method_doc_has_no_figures() -> None:
    text = (ROOT / "docs" / "reports" / "m4-method.md").read_text(encoding="utf-8")
    for pat in _ALLOWED:
        text = re.sub(pat, "", text)
    assert not re.findall(r".{0,20}\d.{0,20}", text)
