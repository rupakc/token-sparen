"""Docs lint shared by the docs tests and the release tooling.

`figure_violations` finds savings/quality/cost results (percentages, multipliers, money, numbers
near result words) outside the Evidence block: results appear only in the rendered Evidence
table, with their confidence intervals (M6 Task 5). Used by `tests/test_user_docs.py` (docs
pages, README) and `scripts/release_notes.py` (release notes). Stdlib only.

The generated E4 report (`bench/report.py`, M5 Task 11) is linted with ``rendered=`` its fresh
re-render from `e4.json`: a section wrapped by `generated_block` is exempt only when the file is
byte-identical to that re-render, so a hand edit or a hand-typed number loses the exemption.
README, docs pages and release notes pass no re-render and never get this exemption.
"""

from __future__ import annotations

import re

# Both marker spellings: docs pages (evidence, /evidence) and the README renderer (evidence:start,
# evidence:end).
_EVIDENCE = re.compile(
    r"<!--\s*evidence(?::start)?\s*-->.*?<!--\s*(?:/evidence|evidence:end)\s*-->", re.S
)
# A generated E4 report section (bench/report.py): exempt only when the caller passes a fresh
# re-render of the whole file from its e4.json and the file is byte-identical to it.
_GENERATED = re.compile(r"<!-- generated:e4-report -->\n.*?<!-- /generated:e4-report -->", re.S)
_FENCE = re.compile(r"^(?P<intro>(?:[^\n]+\n)+)\n```[^\n]*\n.*?\n```", re.M | re.S)
_KEYWORDS = re.compile(
    r"\b(?:saved|savings|reduction|reduces|cheaper|faster|resolved|pass rate|fewer tokens)\b", re.I
)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
_MULTIPLIER = re.compile(r"\b\d+(?:\.\d+)?x\b")
_CURRENCY = re.compile(r"[$€£]\s?\d|\b\d[\d.,]*\s?(?:USD|EUR|GBP|dollars?)\b", re.I)
_NUMBER = re.compile(r"\d")

# Configuration values, limits and the spec's fixed method constants: never results.
_ALLOWED = [
    "95 %",
    "100 %",
    "−5 pp",
    "90 %",
    "15 %",  # compression.min_saving_ratio
    "50 % and 80 %",  # governor budget notices
    "60 % and 80 %",  # governor context notices
    "5 % for compression",  # learner regret target
    "80 %, until measured",  # loop guard assumption
    "about 5 % of",  # loop guard exploration
    "80 % of recent decisions",  # statusline `exploiting`
    ">= 80 %",
    "0.3",
    # README wording (retention and the min_saving_ratio table row)
    "whenever a new one is saved: those older than 14 days",
    "minimum share saved for a replacement",
]


GENERATED_START = "<!-- generated:e4-report -->"
GENERATED_END = "<!-- /generated:e4-report -->"


def generated_block(body: str) -> str:
    """Wrap machine-generated report text (figures rendered from `e4.json`) in the markers."""
    body = body if body.endswith("\n") else body + "\n"
    return f"{GENERATED_START}\n{body}{GENERATED_END}"


def _strip_exempt(text: str, rendered: str | None) -> str:
    """Drop the Evidence block, generated report sections (only when the whole text equals its
    fresh re-render ``rendered``) and fenced examples that announce themselves as made up."""
    text = _EVIDENCE.sub(" ", text)
    if rendered is not None and text == _EVIDENCE.sub(" ", rendered):
        text = _GENERATED.sub(" ", text)

    def keep_fence(m: re.Match[str]) -> str:
        return " " if re.search(r"made up|illustrative", m.group("intro"), re.I) else m.group(0)

    return _FENCE.sub(keep_fence, text)


def figure_violations(text: str, *, rendered: str | None = None) -> list[str]:
    """Savings/quality/cost results (percentages, multipliers, money, numbers near result words).
    ``rendered``: the caller's fresh re-render of this file from its data (the E4 report:
    `bench.report.lint_report`); generated sections are exempt only when ``text`` equals it."""
    text = _strip_exempt(text, rendered)
    text = re.sub(r"^\s*\d+\.\s", " ", text, flags=re.M)  # list numbers
    text = re.sub(r"\s+", " ", text)
    for phrase in _ALLOWED:
        text = text.replace(phrase, " ")
    found = [m.group(0) for pat in (_PERCENT, _MULTIPLIER, _CURRENCY) for m in pat.finditer(text)]
    text = re.sub(r"`[^`]*`", " `code` ", text)  # digits in code spans are not results
    words = list(re.finditer(r"\S+", text))
    for kw in _KEYWORDS.finditer(text):
        idx = next((i for i, w in enumerate(words) if w.start() <= kw.start() < w.end()), None)
        if idx is None:
            continue
        window = words[max(0, idx - 5) : idx + 6]
        if any(_NUMBER.search(w.group(0)) for w in window):
            found.append(" ".join(w.group(0) for w in window))
    return found
