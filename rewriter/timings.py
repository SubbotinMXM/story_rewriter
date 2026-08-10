"""Удаление таймкодов из исходного текста."""

from __future__ import annotations

import re

# Примеры: "0:1414 секундчто", "0:2222 секундывчерашнего", "0:4141 секундаЧайник"
# Без IGNORECASE: иначе [аые]? съедает «А» в «Анна».
_TIMING_SEC = re.compile(r"\d+:\d+\s*секунд(?:а|ы)?\s*")

# Запасные форматы: [00:14], [0:14:03], 00:14:03 в начале строки
_TIMING_BRACKET = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\]\s*")
_TIMING_LINE = re.compile(
    r"(?m)^(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?\s+"
)

_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NL = re.compile(r"\n{3,}")


def strip_timings(text: str) -> str:
    text = _TIMING_SEC.sub("", text)
    text = _TIMING_BRACKET.sub("", text)
    text = _TIMING_LINE.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()
