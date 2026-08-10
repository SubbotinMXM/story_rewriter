"""Пайплайн рерайта: clean → split → rewrite 1–3 → ending → текст."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from rewriter.cancel import CancelToken
from rewriter.glossary import (
    empty_glossary,
    glossary_to_prompt,
    parse_glossary_response,
)
from rewriter.logutil import log
from rewriter.openai_client import DEFAULT_BASE_URL, OpenAIRewriter
from rewriter.split import split_into_parts
from rewriter.timings import strip_timings

ProgressCb = Callable[[float, str], None]

# 2–3 asides на весь рассказ: только на 3 рерайт-части (4-я — новый финал)
ASIDES_PLAN = (1, 1, 1)  # итого 3


@dataclass
class RewriteResult:
    text: str
    parts_source: list[str]
    parts_rewritten: list[str]
    cleaned: str


def desktop_dir() -> Path:
    return Path.home() / "Desktop"


def sanitize_folder_name(text: str, *, max_len: int = 80) -> str:
    """Имя папки из текста оверлея (без запрещённых символов FS)."""
    s = " ".join((text or "").split())
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "-")
    s = s.strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "ролик"


def run_output_dir(overlay_text: str, when: datetime | None = None) -> Path:
    """Папка на Desktop: «оверлей_YYYYMMDD-HHMMSS»."""
    when = when or datetime.now()
    name = f"{sanitize_folder_name(overlay_text)}_{when.strftime('%Y%m%d-%H%M%S')}"
    path = desktop_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def finalize_run_dir(
    out_dir: Path, overlay_text: str, when: datetime | None = None
) -> Path:
    """Переименовать папку прогона под время окончания рендера."""
    when = when or datetime.now()
    target = (
        desktop_dir()
        / f"{sanitize_folder_name(overlay_text)}_{when.strftime('%Y%m%d-%H%M%S')}"
    )
    if out_dir.resolve() == target.resolve():
        return out_dir
    if target.exists():
        # коллизия секунды — не затираем
        n = 2
        while True:
            alt = target.with_name(f"{target.name}_{n}")
            if not alt.exists():
                target = alt
                break
            n += 1
    out_dir.rename(target)
    return target


def story_output_path(
    now: datetime | None = None, *, out_dir: Path | None = None
) -> Path:
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = out_dir or desktop_dir()
    return base / f"рассказ-{stamp}.txt"


def video_output_path(
    now: datetime | None = None, *, out_dir: Path | None = None
) -> Path:
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = out_dir or desktop_dir()
    return base / f"ролик-{stamp}.mp4"


def audio_output_path(
    now: datetime | None = None, *, out_dir: Path | None = None
) -> Path:
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = out_dir or desktop_dir()
    return base / f"озвучка-{stamp}.mp3"


def save_story_text(text: str, path: Path | None = None) -> Path:
    path = path or story_output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def build_final_text(prefix: str, parts: list[str]) -> str:
    body = "\n".join(p.strip() for p in parts if p.strip())
    return f"{prefix}{body}"


def rewrite_story(
    *,
    source_text: str,
    prefix: str,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
) -> RewriteResult:
    """Рерайт в память (без сохранения .txt на Desktop)."""

    def progress(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(pct, msg)
        else:
            log(msg)

    progress(0.02, "Удаляю таймкоды…")
    cleaned = strip_timings(source_text)
    if not cleaned.strip():
        raise ValueError("После удаления таймкодов текст пустой")
    log(f"Текст без таймкодов: {len(cleaned)} символов, ~{len(cleaned.split())} слов")

    progress(0.05, "Делю на 4 части…")
    parts = split_into_parts(cleaned, 4)
    if not any(p.strip() for p in parts):
        raise ValueError("Не удалось разделить текст")
    for i, p in enumerate(parts, 1):
        log(f"Часть {i}: {len(p)} символов, ~{len(p.split())} слов")
    log("Часть 4 исходника отброшена — GPT пишет позитивный финал")

    progress(0.06, f"Подключаюсь к GPT ({base_url})…")
    client = OpenAIRewriter(api_key=api_key, model=model, base_url=base_url)
    if cancel:
        cancel.register(client)
    glossary = empty_glossary()
    rewritten: list[str] = []

    try:
        for i in range(3):
            part = parts[i]
            part_no = i + 1
            base = 0.08 + (i / 3) * 0.65
            progress(base, f"Переписываю часть {part_no}/3…")

            if not part.strip():
                rewritten.append("")
                continue

            text = client.rewrite_part(
                part_index=part_no,
                parts_total=3,
                source_fragment=part,
                glossary_block=glossary_to_prompt(glossary),
                asides_count=ASIDES_PLAN[i],
            )
            rewritten.append(text)

            progress(base + 0.1, f"Обновляю glossary после части {part_no}…")
            raw_g = client.update_glossary(
                glossary_json=json.dumps(glossary, ensure_ascii=False),
                source_fragment=part,
                rewritten=text,
            )
            glossary = parse_glossary_response(raw_g, glossary)

        progress(0.78, "Пишу позитивный финал…")
        ending = client.write_ending(
            rewritten_parts=rewritten,
            glossary_block=glossary_to_prompt(glossary),
        )
        rewritten.append(ending)

        progress(0.95, "Склеиваю текст…")
        final = build_final_text(prefix, rewritten)
        progress(1.0, f"Текст готов ({len(final)} символов)")

        return RewriteResult(
            text=final,
            parts_source=parts,
            parts_rewritten=rewritten,
            cleaned=cleaned,
        )
    finally:
        client.close()


# обратная совместимость для старых импортов/тестов
def run_pipeline(**kwargs) -> RewriteResult:
    return rewrite_story(**kwargs)
