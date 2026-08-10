"""Валидация фразы для YouTube-превью (орфография/синтаксис кавычек и т.п.)."""

from __future__ import annotations

import re

# Общий потолок для мастеров #1 (10–15) и #2 (12–24)
MAX_THUMB_WORDS = 24
MIN_THUMB_WORDS = 10


def balance_guillemets(text: str) -> str:
    """Добить парные «» если модель оставила сироту » или «."""
    t = text or ""
    o, c = t.count("«"), t.count("»")
    if c > o:
        t = ("«" * (c - o)) + t
    elif o > c:
        t = t + ("»" * (o - c))
    return t


def strip_all_quotes(text: str) -> str:
    t = re.sub(r"[«»\"“”']", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:!?—–-])", r"\1", t)
    return t.strip()


def normalize_thumb_phrase(raw: str) -> str:
    text = (raw or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    # Не стрипаем «» с краёв — иначе «ЦИТАТА!» → ЦИТАТА!» и валидация валит фразу.
    text = text.strip().strip('"“”')
    text = balance_guillemets(text)
    # единый обрыв: … → ...
    text = text.replace("…", "...")
    text = re.sub(r"\.{4,}", "...", text)
    # если кончается словом+точками без пробела — ок; если !/?/. перед ... — убрать хвост
    text = re.sub(r"[.!?]+(\s*\.\.\.)\s*$", r"\1", text)
    text = re.sub(r"\s*\.\.\.\s*$", " ...", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text.endswith("..."):
        text = text.rstrip(".!?…") + " ..."
    return text.strip()


def content_words(text: str) -> list[str]:
    """Слова без отдельного токена многоточия."""
    return [w for w in (text or "").split() if w not in (".", "..", "...", "…")]


def force_usable_thumb_phrase(text: str) -> str:
    """Гарантировать фразу, пригодную для картинки. Кавычки стараемся сохранить."""
    t = normalize_thumb_phrase(text)
    if not validate_thumb_phrase(t):
        return t

    # 1) только кавычки / скобки — балансируем ещё раз
    t2 = balance_guillemets(t)
    errs = validate_thumb_phrase(t2)
    if not errs:
        return normalize_thumb_phrase(t2)
    quote_only = all("кавыч" in e or "скобк" in e for e in errs)
    if quote_only:
        t3 = strip_all_quotes(t2)
        t3 = clip_to_word_limit(t3, max_words=MAX_THUMB_WORDS)
        if t3 and not validate_thumb_phrase(t3):
            return normalize_thumb_phrase(t3)
        t = t3 or strip_all_quotes(t)

    # 2) длина
    t = clip_to_word_limit(t, max_words=MAX_THUMB_WORDS)
    words = content_words(t)

    # 3) убрать латиницу / мусор
    t = re.sub(r"[A-Za-z|_~^`]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = strip_all_quotes(t)
    t = clip_to_word_limit(t, max_words=MAX_THUMB_WORDS)

    if not t:
        t = "ЖЕСТОКИЙ КОНФЛИКТ В СЕМЬЕ НО ОНА НЕ ЗНАЛА ЧТО Я"
    words = content_words(t)
    if len(words) < MIN_THUMB_WORDS:
        pad = "НО РАСКЛАД УЖЕ ИЗМЕНИЛСЯ В КОРНЕ КОГДА Я".split()
        t = " ".join((words + pad)[:MAX_THUMB_WORDS])
    return normalize_thumb_phrase(clip_to_word_limit(t, max_words=MAX_THUMB_WORDS))


def salvage_thumb_phrase(text: str) -> str:
    """Починить фразу: сначала баланс «», иначе выкинуть кавычки."""
    t = normalize_thumb_phrase(text)
    if not validate_thumb_phrase(t):
        return t
    t2 = balance_guillemets(t)
    if not validate_thumb_phrase(t2):
        return normalize_thumb_phrase(t2)
    t3 = strip_all_quotes(t2)
    t3 = clip_to_word_limit(t3, max_words=MAX_THUMB_WORDS)
    if t3 and not validate_thumb_phrase(t3):
        return normalize_thumb_phrase(t3)
    return force_usable_thumb_phrase(text)


def validate_thumb_phrase(text: str) -> list[str]:
    """Вернуть список проблем (пустой = ок)."""
    errors: list[str] = []
    t = (text or "").strip()
    if not t:
        return ["пустая фраза"]

    if not (t.endswith("...") or t.endswith("…")):
        errors.append("нет обрыва многоточием «...» в конце")

    words = content_words(t)
    if len(words) < MIN_THUMB_WORDS:
        errors.append(
            f"слишком коротко: {len(words)} слов (нужно {MIN_THUMB_WORDS}–{MAX_THUMB_WORDS})"
        )
    elif len(words) > MAX_THUMB_WORDS:
        errors.append(
            f"слишком длинно: {len(words)} слов (нужно {MIN_THUMB_WORDS}–{MAX_THUMB_WORDS})"
        )

    # кавычки
    for open_c, close_c, name in (
        ("«", "»", "«»"),
        ('"', '"', 'двойные "…"'),
        ("'", "'", "одинарные '…'"),
        ("“", "”", "“…”"),
    ):
        if open_c == close_c:
            if t.count(open_c) % 2 != 0:
                errors.append(f"нечётное число кавычек {name}")
        else:
            o, c = t.count(open_c), t.count(close_c)
            if o != c:
                errors.append(f"незакрытые кавычки {name}: открыто {o}, закрыто {c}")

    # скобки
    for open_c, close_c, name in (("(", ")", "()"), ("[", "]", "[]")):
        o, c = t.count(open_c), t.count(close_c)
        if o != c:
            errors.append(f"незакрытые скобки {name}: открыто {o}, закрыто {c}")

    # латинские буквы внутри «слов» — подозрительно для кириллического превью
    if re.search(r"[A-Za-z]", t):
        errors.append("есть латинские буквы (ожидается кириллица)")

    # мусорные символы
    if re.search(r"[|_~^`]", t):
        errors.append("подозрительные служебные символы")

    # двойные пробелы / висячая пунктуация
    if "  " in t:
        errors.append("двойные пробелы")
    if re.search(r"\s[,.!?:;]", t):
        # пробел перед ... в конце — норма («Я ...»)
        body = re.sub(r"\s*(\.\.\.|…)\s*$", "", t)
        if re.search(r"\s[,.!?:;]", body):
            errors.append("пробел перед знаком препинания")
    # «...» в конце ок; иначе 3+ знака подряд — ошибка
    body = re.sub(r"(\.\.\.|…)\s*$", "", t)
    if re.search(r"[,.!?]{3,}", body):
        errors.append("слишком много знаков препинания подряд")

    return errors


def clip_to_word_limit(text: str, *, max_words: int = MAX_THUMB_WORDS) -> str:
    t = (text or "").strip()
    words = content_words(t)
    if len(words) > max_words:
        words = words[:max_words]
    out = " ".join(words).rstrip(".!?…") + " ..."
    return re.sub(r"\s+", " ", out).strip()
