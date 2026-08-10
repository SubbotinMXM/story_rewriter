"""Чекпоинты полного флоу: продолжение с места падения."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rewriter.logutil import log

Stage = Literal["none", "rewrite_done", "tts_done", "completed"]

RUN_DIR = Path(__file__).resolve().parent.parent / ".run"
STATE_PATH = RUN_DIR / "state.json"
TEXT_PATH = RUN_DIR / "story.txt"
AUDIO_PATH = RUN_DIR / "voice.mp3"


@dataclass
class Checkpoint:
    stage: Stage
    source_hash: str
    prefix_hash: str
    text_chars: int = 0
    audio_bytes: int = 0
    last_error: str = ""
    next_stage_label: str = ""

    @property
    def can_resume(self) -> bool:
        if self.stage == "rewrite_done":
            return TEXT_PATH.is_file() and TEXT_PATH.stat().st_size > 0
        if self.stage == "tts_done":
            return (
                TEXT_PATH.is_file()
                and AUDIO_PATH.is_file()
                and AUDIO_PATH.stat().st_size > 0
            )
        return False

    @property
    def resume_button_label(self) -> str:
        if self.stage == "rewrite_done":
            return "Продолжить с озвучки"
        if self.stage == "tts_done":
            return "Продолжить со сборки видео"
        return "Создать ролик"


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def ensure_run_dir() -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


def clear_checkpoint() -> None:
    for path in (STATE_PATH, TEXT_PATH, AUDIO_PATH):
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    log("Чекпоинт очищен")


def load_checkpoint() -> Checkpoint | None:
    if not STATE_PATH.is_file():
        return None
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cp = Checkpoint(
        stage=raw.get("stage") or "none",  # type: ignore[arg-type]
        source_hash=str(raw.get("source_hash") or ""),
        prefix_hash=str(raw.get("prefix_hash") or ""),
        text_chars=int(raw.get("text_chars") or 0),
        audio_bytes=int(raw.get("audio_bytes") or 0),
        last_error=str(raw.get("last_error") or ""),
        next_stage_label=str(raw.get("next_stage_label") or ""),
    )
    if cp.stage in {"rewrite_done", "tts_done"} and not cp.can_resume:
        return None
    if cp.stage not in {"rewrite_done", "tts_done"}:
        return None
    return cp


def save_state(**fields: Any) -> None:
    ensure_run_dir()
    data: dict[str, Any] = {}
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data.update(fields)
    STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mark_rewrite_done(*, text: str, source_hash: str, prefix_hash: str) -> None:
    ensure_run_dir()
    TEXT_PATH.write_text(text, encoding="utf-8")
    if AUDIO_PATH.is_file():
        AUDIO_PATH.unlink(missing_ok=True)
    save_state(
        stage="rewrite_done",
        source_hash=source_hash,
        prefix_hash=prefix_hash,
        text_chars=len(text),
        audio_bytes=0,
        last_error="",
        next_stage_label="озвучка",
    )
    log(f"Чекпоинт: rewrite_done ({len(text)} символов)")


def mark_tts_done(*, audio_bytes: int) -> None:
    save_state(
        stage="tts_done",
        audio_bytes=audio_bytes,
        last_error="",
        next_stage_label="сборка видео",
    )
    log(f"Чекпоинт: tts_done ({audio_bytes} bytes)")


def mark_failed(error: str) -> None:
    save_state(last_error=error[:2000])
    log(f"Чекпоинт: ошибка зафиксирована ({error[:120]})")


def mark_completed() -> None:
    clear_checkpoint()
    log("Чекпоинт: completed, очищено")


def read_checkpoint_text() -> str:
    return TEXT_PATH.read_text(encoding="utf-8")
