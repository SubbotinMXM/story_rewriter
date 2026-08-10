"""Утилиты: ffprobe, пути."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from compositor.defaults import AUDIO_EXTS, VIDEO_EXTS


class FFmpegNotFoundError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        for candidate in (
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ):
            if Path(candidate).exists():
                return candidate
        raise FFmpegNotFoundError(
            "ffmpeg не найден. Установи: brew install ffmpeg"
        )
    return path


def find_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        for candidate in (
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
        ):
            if Path(candidate).exists():
                return candidate
        raise FFmpegNotFoundError(
            "ffprobe не найден. Установи: brew install ffmpeg"
        )
    return path


def probe_duration(path: Path) -> float:
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"Нулевая длительность: {path}")
    return duration


def list_videos(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {folder}")
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not files:
        raise FileNotFoundError(f"В папке нет видео: {folder}")
    return files


def list_videos_soft(folder: Path) -> list[Path]:
    """Как list_videos, но без исключений: нет папки/файлов → []."""
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


def has_audio_stream(path: Path) -> bool:
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS
