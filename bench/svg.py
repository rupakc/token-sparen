"""Small stdlib SVG charts for the public E4 report (M5 Task 11).

`forest` draws one estimate with its 95 % CI per row against a reference line; `scatter` places
labelled points (the cost/quality Pareto view). Every mark has a text label, the data palette is
two tones from the Okabe-Ito colour-blind-safe set, and a ``prefers-color-scheme`` rule inside the
SVG swaps the neutrals for dark mode. Output is a well-formed standalone SVG document.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape

PRIMARY = "#0072B2"  # Okabe-Ito blue: estimates
ACCENT = "#D55E00"  # Okabe-Ito vermillion: reference line / highlighted point
INK, MUTED, PAPER = "#1A1A1A", "#6B6B6B", "#FFFFFF"
NEUTRALS = (INK, MUTED, PAPER)
DARK = {"ink": "#EDEDED", "muted": "#A6A6A6", "paper": "#141414"}

WIDTH = 640
LABEL_W = 240
LABEL_CHARS = 34  # longer labels are cut (full text in the row's <title>)
PAD = 24
ROW_H = 36


@dataclass(frozen=True)
class Row:
    label: str
    point: float
    lo: float
    hi: float
    n: int


@dataclass(frozen=True)
class Point:
    label: str
    x: float
    y: float


def _style() -> str:
    return (
        "<style>"
        f".ink{{fill:{INK}}} .muted{{fill:{MUTED}}} .paper{{fill:{PAPER}}} "
        f".axis{{stroke:{MUTED}}} .est{{fill:{PRIMARY};stroke:{PRIMARY}}} "
        f".ref{{stroke:{ACCENT}}} .refl{{fill:{ACCENT}}} "
        "text{font-family:system-ui,sans-serif;font-size:12px}"
        "@media (prefers-color-scheme: dark){"
        f".ink{{fill:{DARK['ink']}}} .muted{{fill:{DARK['muted']}}} "
        f".paper{{fill:{DARK['paper']}}} .axis{{stroke:{DARK['muted']}}}"
        "}</style>"
    )


def _short(label: str) -> str:
    return label if len(label) <= LABEL_CHARS else label[: LABEL_CHARS - 1] + "\u2026"


def _num(x: float) -> str:
    return f"{x:.4g}"


def _doc(height: int, title: str, body: list[str]) -> str:
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img">'
        f"<title>{escape(title)}</title>{_style()}"
        f'<rect class="paper" x="0" y="0" width="{WIDTH}" height="{height}"/>'
        f'<text class="ink" x="{PAD}" y="{PAD}" font-weight="bold">{escape(title)}</text>'
    )
    return head + "".join(body) + "</svg>\n"


def _scale(lo: float, hi: float, a: float, b: float) -> tuple[float, float, float]:
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        lo, hi = (lo - 1, lo + 1) if math.isfinite(lo) else (-1.0, 1.0)
    span = hi - lo
    return lo - 0.05 * span, hi + 0.05 * span, (b - a)


def forest(rows: Sequence[Row], *, ref: float, title: str) -> str:
    """One row per estimate: point, 95 % CI whisker, text label with the numbers and n; a
    vertical reference line at ``ref`` (e.g. ratio 1 or the −5 pp margin)."""
    top = PAD + 20
    height = top + ROW_H * max(1, len(rows)) + 2 * PAD
    values = [ref, *(v for r in rows for v in (r.lo, r.hi, r.point))]
    lo, hi, width = _scale(min(values), max(values), LABEL_W, WIDTH - PAD)

    def x(v: float) -> float:
        return LABEL_W + (v - lo) / (hi - lo) * width

    body = [
        f'<line class="ref" x1="{x(ref):.1f}" y1="{top - 8}" x2="{x(ref):.1f}" '
        f'y2="{height - 2 * PAD + 4}" stroke-width="1.5" stroke-dasharray="4 3"/>',
        f'<text class="refl" x="{x(ref):.1f}" y="{height - PAD + 2}" '
        f'text-anchor="middle">reference {_num(ref)}</text>',
    ]
    for i, r in enumerate(rows):
        y = top + ROW_H * i + ROW_H / 2
        body += [
            f"<g><title>{escape(r.label)}</title>",
            f'<text class="ink" x="{PAD}" y="{y - 2:.1f}">{escape(_short(r.label))}</text>',
            f'<text class="muted" x="{PAD}" y="{y + 11:.1f}" font-size="10">'
            f"{_num(r.point)} [{_num(r.lo)}, {_num(r.hi)}] n={r.n}</text>",
            f'<line class="est" x1="{x(r.lo):.1f}" y1="{y:.1f}" x2="{x(r.hi):.1f}" '
            f'y2="{y:.1f}" stroke-width="2"/>',
            f'<circle class="est" cx="{x(r.point):.1f}" cy="{y:.1f}" r="4"/></g>',
        ]
    return _doc(height, title, body)


def scatter(points: Sequence[Point], *, title: str) -> str:
    """Labelled points on x/y axes (cost ratio vs resolved difference: the Pareto view)."""
    height = 360
    left, right, top, bottom = PAD + 40, WIDTH - PAD, PAD + 20, height - PAD - 20
    xs = [p.x for p in points] or [0.0]
    ys = [p.y for p in points] or [0.0]
    x0, x1, xw = _scale(min(xs), max(xs), left, right)
    y0, y1, yh = _scale(min(ys), max(ys), top, bottom)

    def px(v: float) -> float:
        return left + (v - x0) / (x1 - x0) * xw

    def py(v: float) -> float:
        return bottom - (v - y0) / (y1 - y0) * yh

    body = [
        f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}"/>',
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{bottom}"/>',
        f'<text class="muted" x="{right}" y="{bottom + 16}" text-anchor="end">'
        f"x: {_num(x0)} to {_num(x1)}</text>",
        f'<text class="muted" x="{PAD}" y="{top - 4}">y: {_num(y0)} to {_num(y1)}</text>',
    ]
    for p in points:
        body += [
            f'<circle class="est" cx="{px(p.x):.1f}" cy="{py(p.y):.1f}" r="5"/>',
            f'<text class="ink" x="{px(p.x) + 8:.1f}" y="{py(p.y) + 4:.1f}">'
            f"{escape(p.label)} ({_num(p.x)}, {_num(p.y)})</text>",
        ]
    return _doc(height, title, body)
