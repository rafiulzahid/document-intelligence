"""Turn positioned words (from a PDF text layer or OCR) into layout-preserving lines.

Words in the same visual row are joined left to right, even when they belong to different
columns, so table rows like "Gorgonzola Telino   $10.00   20   $200.00" stay on one line.
"""
from __future__ import annotations

from statistics import median
from typing import Iterable, Optional, Sequence

# a word: (x0, y0, x1, y1, text, confidence or None)
Word = tuple[float, float, float, float, str, Optional[float]]


def words_to_lines(words: Iterable[Word]) -> list[list[Word]]:
    ws = [w for w in words if str(w[4]).strip()]
    if not ws:
        return []
    h = median(max(1.0, w[3] - w[1]) for w in ws)
    ws.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list[Word]] = []
    centers: list[float] = []
    for w in ws:
        c = (w[1] + w[3]) / 2
        if lines and abs(c - centers[-1]) <= 0.5 * h:
            lines[-1].append(w)
            centers[-1] = sum((x[1] + x[3]) / 2 for x in lines[-1]) / len(lines[-1])
        else:
            lines.append([w]); centers.append(c)
    return [sorted(l, key=lambda w: w[0]) for l in lines]


def lines_to_text(lines: Sequence[Sequence[Word]]) -> str:
    if not lines:
        return ""
    h = median(max(1.0, w[3] - w[1]) for l in lines for w in l)
    out = []
    for l in lines:
        parts = [str(l[0][4]).strip()]
        for prev, w in zip(l, l[1:]):
            gap = w[0] - prev[2]
            parts.append("   " if gap > 1.2 * h else " ")
            parts.append(str(w[4]).strip())
        out.append("".join(parts))
    return "\n".join(out)


def words_to_text(words: Iterable[Word]) -> str:
    return lines_to_text(words_to_lines(words))


def mean_confidence(words: Iterable[Word]) -> Optional[float]:
    confs = [w[5] for w in words if w[5] is not None and w[5] >= 0]
    return round(sum(confs) / len(confs), 4) if confs else None
