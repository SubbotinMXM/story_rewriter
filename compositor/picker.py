"""Выбор клипов и случайных эффектов."""

from __future__ import annotations

import random
from pathlib import Path

from compositor.defaults import BROLL_COUNT_MAX, BROLL_COUNT_MIN
from compositor.utils import list_videos, list_videos_soft

# Лёгкие фильтры только на b-roll фон
EFFECTS: dict[str, str] = {
    "vignette": "vignette=PI/5",
    "grain": "noise=alls=8:allf=t",
    "warm": "eq=saturation=1.05:gamma_r=1.03:gamma_b=0.97",
    "cool": "eq=saturation=0.95:gamma_b=1.04:gamma_r=0.98",
    "soft": "gblur=sigma=0.6,eq=contrast=1.02",
    "fade_sat": "eq=saturation=0.85:brightness=0.02",
}


def pick_head(folder: Path, rng: random.Random | None = None) -> Path:
    rng = rng or random.Random()
    files = list_videos(folder)
    return rng.choice(files)


def pick_outro(folder: Path | None, rng: random.Random | None = None) -> Path | None:
    """Случайное видео из папки аутро. None если путь пуст / нет файлов."""
    if folder is None or not folder.is_dir():
        return None
    files = list_videos_soft(folder)
    if not files:
        return None
    rng = rng or random.Random()
    return rng.choice(files)


def pick_broll(folder: Path, rng: random.Random | None = None) -> list[Path]:
    rng = rng or random.Random()
    files = list_videos(folder)
    count = rng.randint(BROLL_COUNT_MIN, BROLL_COUNT_MAX)
    if len(files) >= count:
        return rng.sample(files, count)
    # меньше файлов — берём все + добираем с повтором
    picked = list(files)
    while len(picked) < count:
        picked.append(rng.choice(files))
    rng.shuffle(picked)
    return picked[:count]


def pick_effect(rng: random.Random | None = None) -> tuple[str, str]:
    rng = rng or random.Random()
    name = rng.choice(list(EFFECTS.keys()))
    return name, EFFECTS[name]
