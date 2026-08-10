"""Глоссарий замен между частями."""

from __future__ import annotations

import json
from typing import Any


def empty_glossary() -> dict[str, Any]:
    return {
        "names": {},
        "places": {},
        "details": {},
        "relations": [],
        "plot_notes": [],
    }


def glossary_to_prompt(glossary: dict[str, Any]) -> str:
    if not any(
        [
            glossary.get("names"),
            glossary.get("places"),
            glossary.get("details"),
            glossary.get("relations"),
            glossary.get("plot_notes"),
        ]
    ):
        return "Глоссарий пока пуст — это первая часть. Создай замены и далее их придерживайся."

    return (
        "Глоссарий соответствий (используй строго):\n"
        + json.dumps(glossary, ensure_ascii=False, indent=2)
    )


def parse_glossary_response(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return fallback
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return fallback

    result = empty_glossary()
    for key in ("names", "places", "details"):
        src = data.get(key) or {}
        if isinstance(src, dict):
            result[key] = {str(k): str(v) for k, v in src.items() if k and v}
        # merge old
        result[key] = {**(fallback.get(key) or {}), **result[key]}

    for key in ("relations", "plot_notes"):
        items = data.get(key) or []
        old = list(fallback.get(key) or [])
        if isinstance(items, list):
            merged = old[:]
            for item in items:
                s = str(item).strip()
                if s and s not in merged:
                    merged.append(s)
            result[key] = merged
        else:
            result[key] = old

    return result
