"""Documentation guards."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "specs" / "2026-11-25-token-sparen-design.md"

# Exact phrases in which digits are allowed (method parameters). Everything else must be digit-free
# apart from ISO dates, section numbers (§10.6) and eval/milestone ids (E1, M4) and the one path
# below.
_PHRASES = [
    "≥ 8 project groups",
    "≥ 30 trims",
    "0.2 · τ",
    "95 %",
    "100 %",
    "evals/e2/run.py",
    "docs/reports/m4-method.md",
    "<state root>/hillclimb/confirm-m4.json",
]
_STRUCTURAL = [r"\b\d{4}-\d{2}-\d{2}\b", r"§\d+(?:\.\d+)*(?:[/–-]§?\d+(?:\.\d+)*)*", r"\b[EM]\d\b"]
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "twenty|thirty|forty|fifty|sixty|hundred|dozen"
)
_COUNTED = r"(?:sessions?|groups?|trims?|tokens?|projects?|files?|runs?|resamples?|values?)"


def _violations(text: str) -> list[str]:
    for phrase in _PHRASES:
        text = text.replace(phrase, " ")
    for pat in _STRUCTURAL:
        text = re.sub(pat, " ", text)
    found = [m.group(0) for m in re.finditer(r".{0,20}\d.{0,20}", text)]
    found += [
        m.group(0)
        for m in re.finditer(rf"\b(?:{_NUMBER_WORDS})\b\s+(?:\w+\s+)?{_COUNTED}\b", text, re.I)
    ]
    return found


def _m4_log_entry() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("**2026-11-27 (M4).**")
    end = text.find("\n\n", start)
    return text[start:end]


def test_method_doc_has_no_figures() -> None:
    text = (ROOT / "docs" / "reports" / "m4-method.md").read_text(encoding="utf-8")
    assert _violations(text) == []


def test_spec_m4_revision_has_no_figures() -> None:
    assert _violations(_m4_log_entry()) == []
