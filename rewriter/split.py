"""Деление текста на примерно равные части по границе предложения."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r".*?[.!?…](?:[\"»”']+)?(?=\s+|$)", re.DOTALL)


def split_into_parts(text: str, n: int = 4) -> list[str]:
    text = text.strip()
    if not text:
        return [""] * n

    words = text.split()
    if len(words) <= n:
        parts = words + [""] * (n - len(words))
        return parts

    target = len(words) / n
    parts: list[str] = []
    start = 0

    for i in range(n - 1):
        ideal = int(round(target * (i + 1)))
        end = _best_break(words, start, ideal, len(words) - (n - i - 1))
        parts.append(" ".join(words[start:end]).strip())
        start = end

    parts.append(" ".join(words[start:]).strip())
    return parts


def _best_break(words: list[str], start: int, ideal: int, hard_max: int) -> int:
    """Ищем конец предложения рядом с ideal, не дальше hard_max."""
    ideal = max(start + 1, min(ideal, hard_max))
    hard_max = max(ideal, hard_max)

    # Окно поиска вокруг ideal
    lo = max(start + 1, ideal - 80)
    hi = min(hard_max, ideal + 80)

    best = ideal
    best_dist = abs(ideal - best)

    for idx in range(lo, hi + 1):
        if idx <= start or idx > hard_max:
            continue
        token = words[idx - 1]
        if re.search(r"[.!?…][\"»”']?$", token):
            dist = abs(idx - ideal)
            if dist < best_dist:
                best = idx
                best_dist = dist

    return max(start + 1, min(best, hard_max))
