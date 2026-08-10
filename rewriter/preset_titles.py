"""Очередь заголовков пресета: unused → used, цикл после исчерпания."""

from __future__ import annotations

import json
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_USER_DIR = _ROOT / "user_presets"
_LOCK = threading.Lock()


def titles_path(preset_id: str) -> Path:
    return _USER_DIR / preset_id.strip() / "titles.txt"


def used_state_path(preset_id: str) -> Path:
    return _USER_DIR / preset_id.strip() / "titles_used.json"


def has_title_catalog(preset_id: str) -> bool:
    p = titles_path(preset_id)
    return p.is_file() and bool(p.read_text(encoding="utf-8").strip())


def load_titles(preset_id: str) -> list[str]:
    path = titles_path(preset_id)
    if not path.is_file():
        raise FileNotFoundError(f"Нет titles.txt у пресета {preset_id}")
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    if not out:
        raise ValueError(f"Пустой каталог заголовков: {path}")
    return out


def _load_used(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        used = data.get("used")
        if isinstance(used, list):
            return [str(x) for x in used]
    if isinstance(data, list):
        return [str(x) for x in data]
    return []


def _save_used(path: Path, used: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"used": used}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def next_title(preset_id: str) -> str:
    """Следующий неиспользованный заголовок по порядку; после исчерпания — цикл."""
    pid = (preset_id or "").strip()
    if not pid:
        raise ValueError("пустой preset_id")
    with _LOCK:
        titles = load_titles(pid)
        state_path = used_state_path(pid)
        used = _load_used(state_path)
        used_set = set(used)
        pick = None
        for t in titles:
            if t not in used_set:
                pick = t
                break
        if pick is None:
            used = []
            pick = titles[0]
        used.append(pick)
        _save_used(state_path, used)
        return pick


def peek_remaining(preset_id: str) -> list[str]:
    """Оставшиеся (не использованные) в текущем цикле — для отладки."""
    titles = load_titles(preset_id)
    used = set(_load_used(used_state_path(preset_id)))
    return [t for t in titles if t not in used]
