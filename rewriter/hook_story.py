"""Рассказ с нуля по хуку: план (1–14) → чанки → мета."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rewriter.cancel import CancelToken
from rewriter.logutil import log
from rewriter.openai_client import DEFAULT_BASE_URL, OpenAIRewriter
from rewriter.profession_story import (
    StoryMeta,
    _strip_part_chrome,
    _tail,
    parse_meta_block,
    parse_story_and_meta,
    word_count,
)

ProgressCb = Callable[[float, str], None]

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "hook_story_plan_v1.txt"

TARGET_MIN_WORDS = 8_000
TARGET_MAX_WORDS = 10_000
MAX_STORY_PARTS = 8
WORDS_PER_PART = 2_200

HOOK_PLACEHOLDER = "[ВСТАВИТЬ ХУК]"

_SECTION_HEADER_RE = re.compile(r"(?m)^(\d{1,2})\.\s+\S")


@dataclass
class HookStoryResult:
    text: str
    meta: StoryMeta
    hook: str
    word_count: int
    parts: list[str]
    plan: str = ""


def load_master_prompt(path: Path | None = None) -> str:
    p = path or PROMPT_PATH
    return p.read_text(encoding="utf-8").strip()


def fill_hook_prompt(template: str, hook: str) -> str:
    """Подставить хук вместо [ВСТАВИТЬ ХУК]."""
    h = (hook or "").strip()
    if not h:
        raise ValueError("Хук пустой")
    if HOOK_PLACEHOLDER in template:
        return template.replace(HOOK_PLACEHOLDER, h, 1)
    # fallback: блок после «ИСХОДНЫЙ ХУК»
    pattern = re.compile(
        r"(ИСХОДНЫЙ ХУК\s*\n\n)(.*?)(\n\nПример:)",
        re.DOTALL,
    )
    if pattern.search(template):
        return pattern.sub(rf"\g<1>{h}\g<3>", template, count=1)
    return f"ИСХОДНЫЙ ХУК\n\n{h}\n\n{template}"


def extract_plan_section(plan: str, section_num: int) -> str:
    """Вырезать секцию N. … до следующей нумерованной секции."""
    text = plan or ""
    headers = list(_SECTION_HEADER_RE.finditer(text))
    start: int | None = None
    end: int | None = None
    for i, m in enumerate(headers):
        num = int(m.group(1))
        if num == section_num and start is None:
            start = m.start()
            if i + 1 < len(headers):
                end = headers[i + 1].start()
            break
    if start is None:
        return ""
    chunk = text[start:end].strip() if end is not None else text[start:].strip()
    return chunk


def writing_instruction_from_plan(plan: str) -> str:
    """Секция 14 + хвост плана (доп. требования к языку), если есть."""
    s14 = extract_plan_section(plan, 14)
    if s14:
        return s14
    # fallback: искать по заголовку
    m = re.search(
        r"(?im)^14\.\s*ИНСТРУКЦИЯ ДЛЯ НАПИСАНИЯ.*",
        plan or "",
    )
    if m:
        return (plan or "")[m.start() :].strip()
    return (plan or "").strip()


def characters_bible_from_plan(plan: str) -> str:
    """Персонажи из плана (секция 4) + основа истории (2) для continuity."""
    parts: list[str] = []
    for n in (4, 2, 6):
        block = extract_plan_section(plan, n)
        if block:
            parts.append(block)
    return "\n\n".join(parts).strip() or (plan or "")[:4000]


def save_story_plan(plan: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((plan or "").strip() + "\n", encoding="utf-8")
    return path


def generate_hook_story(
    *,
    hook: str,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    prefix: str = "",
    on_progress: ProgressCb | None = None,
    prompt_path: Path | None = None,
    cancel: CancelToken | None = None,
) -> HookStoryResult:
    """План по хуку → части рассказа → мета. В text только тело (+prefix)."""

    def progress(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(pct, msg)
        else:
            log(msg)

    hook_clean = (hook or "").strip()
    if not hook_clean:
        raise ValueError("Хук пустой")

    template = load_master_prompt(prompt_path)
    master = fill_hook_prompt(template, hook_clean)
    log(f"Хук: {hook_clean[:120]}{'…' if len(hook_clean) > 120 else ''}")

    progress(0.02, "Подключаюсь к GPT…")
    client = OpenAIRewriter(api_key=api_key, model=model, base_url=base_url)
    if cancel:
        cancel.register(client)

    try:
        progress(0.05, "Строю подробный план по хуку…")
        plan = client.complete(
            label="hook-plan",
            system=(
                "Ты — профессиональный сценарист длинных русскоязычных "
                "аудиорассказов для YouTube. Отвечай только подробным "
                "планом-сценарием по формату пользователя. Не пиши сам рассказ."
            ),
            user=master,
        )
        if not plan.strip():
            raise RuntimeError("Пустой план по хуку")

        write_instr = writing_instruction_from_plan(plan)
        bible = characters_bible_from_plan(plan)
        progress(0.14, "План готов, пишу рассказ по плану…")

        parts: list[str] = []
        running_summary = ""
        total_words = 0
        part_i = 0

        while total_words < TARGET_MIN_WORDS and part_i < MAX_STORY_PARTS:
            part_i += 1
            remaining = TARGET_MAX_WORDS - total_words
            target_now = min(WORDS_PER_PART, max(1200, remaining))
            is_first = part_i == 1
            must_finish = (
                total_words + target_now >= TARGET_MIN_WORDS
                or part_i == MAX_STORY_PARTS
            )

            base_pct = 0.15 + (part_i - 1) / MAX_STORY_PARTS * 0.70
            progress(
                base_pct,
                f"Пишу часть {part_i} (~{target_now} слов, сейчас {total_words})…",
            )

            prev_tail = _tail(parts[-1]) if parts else ""
            finish_rule = (
                "Это ПОСЛЕДНЯЯ часть: заверши сюжет по плану (торжество "
                "справедливости, последствия для виновных). Не обрывай на полуслове."
                if must_finish
                else (
                    "Это НЕ финал: закончи на естественном переходе к следующей "
                    "сцене. Не раскрывай всё разоблачение и не пиши эпилог."
                )
            )
            hook_rule = (
                (
                    "Начни рассказ СРАЗУ с исходного хука (первая сцена = хук):\n"
                    f"«{hook_clean}»\n"
                    "Не пиши вступление до хука, не ставь заголовок."
                )
                if is_first
                else (
                    "Продолжай сразу с места остановки предыдущей части. "
                    "Не пересказывай уже написанное, не начинай заново."
                )
            )

            user = (
                f"Исходный хук:\n{hook_clean}\n\n"
                f"Полный план-сценарий (соблюдай строго):\n{plan}\n\n"
                f"Инструкция для написания (из плана):\n{write_instr}\n\n"
                f"Библия персонажей / факты из плана:\n{bible}\n\n"
                f"Краткое содержание уже написанного:\n"
                f"{running_summary or '(пока ничего)'}\n\n"
                f"Хвост предыдущей части:\n{prev_tail or '(это первая часть)'}\n\n"
                f"Задача: напиши часть {part_i} рассказа объёмом примерно "
                f"{target_now} слов (допустимо ±15%).\n"
                f"{hook_rule}\n"
                f"{finish_rule}\n"
                "Пиши только художественный текст этой части от третьего лица. "
                "Без заголовков глав/частей, без нумерации актов, без мета-блока, "
                "без названий и описаний для YouTube."
            )

            chunk = client.complete(
                label=f"hook-part-{part_i}",
                system=(
                    "Ты пишешь фрагмент длинного жизненного аудиорассказа строго "
                    "по готовому плану. Целевой объём всего рассказа "
                    f"{TARGET_MIN_WORDS}–{TARGET_MAX_WORDS} слов. "
                    "В ответе только текст фрагмента, без пояснений."
                ),
                user=user,
            )
            chunk = _strip_part_chrome(chunk)
            if not chunk:
                raise RuntimeError(f"Пустая часть рассказа #{part_i}")
            parts.append(chunk)
            total_words = word_count("\n\n".join(parts))
            log(f"Часть {part_i}: ~{word_count(chunk)} слов, всего ~{total_words}")

            progress(base_pct + 0.03, f"Обновляю саммари после части {part_i}…")
            running_summary = client.complete(
                label=f"hook-summary-{part_i}",
                system=(
                    "Ты ведёшь running summary для continuity. "
                    "Только сжатое содержание, без художественного текста."
                ),
                user=(
                    f"Предыдущее саммари:\n{running_summary or '(пусто)'}\n\n"
                    f"Новая часть:\n{_tail(chunk, max_chars=6000)}\n\n"
                    "Обнови саммари (факты, имена, тайны, открытые вопросы). "
                    "До 500 слов."
                ),
            )

            if must_finish and total_words >= TARGET_MIN_WORDS * 0.85:
                break
            if total_words >= TARGET_MIN_WORDS:
                break

        story_body = "\n\n".join(parts).strip()
        total_words = word_count(story_body)
        progress(0.88, f"Рассказ склеен (~{total_words} слов), собираю мета…")

        meta_raw = client.complete(
            label="hook-meta",
            system=(
                "Ты маркетолог YouTube-аудиорассказов. "
                "Выдай только нумерованный мета-блок 1–6, без текста рассказа."
            ),
            user=(
                f"Хук: {hook_clean}\n\n"
                f"Саммари рассказа:\n{running_summary}\n\n"
                f"Фрагмент начала:\n{_tail(parts[0], max_chars=1200) if parts else ''}\n\n"
                f"Фрагмент конца:\n{_tail(parts[-1], max_chars=1200) if parts else ''}\n\n"
                "Сформируй:\n"
                "1. Пять кликбейтных названий длиной 2–4 слова.\n"
                "2. Пять заголовков для YouTube длиной до 100 символов.\n"
                "3. Краткое эмоциональное описание ролика объёмом 500–700 знаков.\n"
                "4. Пять вариантов текста для превью длиной не более четырёх слов.\n"
                "5. Краткое содержание рассказа в одном абзаце.\n"
                "6. Список главных сюжетных поворотов для проверки логики.\n"
                "Строго в этом порядке, с нумерацией 1.–6."
            ),
        )
        meta = parse_meta_block(meta_raw)
        if meta.is_empty():
            _, meta = parse_story_and_meta(meta_raw)

        body_only, maybe_meta = parse_story_and_meta(story_body)
        if not maybe_meta.is_empty() and meta.is_empty():
            meta = maybe_meta
        if body_only.strip():
            story_body = body_only

        if prefix:
            final = f"{prefix}{story_body}"
        else:
            final = story_body

        wc = word_count(story_body)
        progress(1.0, f"Рассказ готов (~{wc} слов)")
        if wc < TARGET_MIN_WORDS:
            log(
                f"Внимание: слов ~{wc} < целевых {TARGET_MIN_WORDS} "
                f"(частей {len(parts)}, лимит {MAX_STORY_PARTS})"
            )

        return HookStoryResult(
            text=final,
            meta=meta,
            hook=hook_clean,
            word_count=wc,
            parts=parts,
            plan=plan,
        )
    finally:
        client.close()
