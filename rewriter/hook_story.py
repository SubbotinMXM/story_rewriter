"""Рассказ с нуля по хуку: план (9–12 частей) → текст по частям → мета."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rewriter.cancel import CancelToken
from rewriter.logutil import log
from rewriter.openai_client import (
    DEFAULT_BASE_URL,
    OpenAIRewriter,
    is_content_policy_error,
)
from rewriter.profession_story import (
    StoryMeta,
    _strip_part_chrome,
    _tail,
    parse_meta_block,
    parse_story_and_meta,
    word_count,
)

ProgressCb = Callable[[float, str], None]

TARGET_MIN_WORDS = 10_000
TARGET_MAX_WORDS = 10_000
TARGET_TOTAL_WORDS = 10_000
WORDS_PER_PART_DEFAULT = 900
WORDS_PER_PART_MIN = 600
WORDS_PER_PART_MAX = 1_100
CONTINUE_CHUNK_WORDS = 1_000
MAX_CONTINUE_CHUNKS = 4

PLAN_SYSTEM = (
    "Ты — сценарист длинных русскоязычных аудиорассказов для YouTube. "
    "Отвечай по запросу пользователя. Не пиши сам рассказ — только план частей."
)

STORY_SYSTEM = (
    "Ты — автор длинных русскоязычных аудиорассказов для YouTube "
    "(семейная драма для женщин старшего возраста). "
    "Один сквозной сюжет: конфликт нарастает от части к части, "
    "полная развязка — только в финале. "
    "Пиши только художественный текст — без пояснений, заголовков, "
    "markdown и мета-блоков."
)

_PART_HEADER_RE = re.compile(
    r"(?im)^(?:\*{0,2})?(?:часть|part)\s+(\d{1,2})\b"
)
_PCT_RE = re.compile(r"(\d{1,2})\s*%")


def build_plan_prompt(hook: str) -> str:
    topic = (hook or "").strip()
    if not topic:
        raise ValueError("Хук пустой")
    return (
        f"Как бы ты разделил на 9-12 условных частей "
        f"(в том числе ПРОПОРЦИОНАЛЬНО) сценарий для видоса на ютубе "
        f"на тему {topic}\n"
        "Ключевое, что это драматический рассказ для женщин 65+ лет, "
        "где есть мощный конфликт, а в итоге торжествует справедливость.\n\n"
        "Важно для структуры плана:\n"
        "— Части 1–3: завязка и нарастание удара (без примирения и без финала).\n"
        "— Средние части: новые осложнения, улики, предательства, откаты — "
        "конфликт УСИЛИВАЕТСЯ, а не гаснет.\n"
        "— Предпоследняя часть: максимальное напряжение, кульминация на подходе, "
        "но главный конфликт ещё НЕ решён.\n"
        "— Только последняя часть: публичная развязка, справедливость, "
        "последствия для виновных, короткий эпилог «новой жизни».\n"
        "— В плане каждой части (кроме последней) явно укажи, какое НОВОЕ "
        "осложнение она вносит; не повторяй одну и ту же мысль "
        "(например «прощает, но не доверяет») в нескольких частях."
    )


def parse_plan_parts(plan: str) -> list[str]:
    """Выделить части из ответа шага 1 (нумерация / «Часть N»)."""
    text = (plan or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    starts: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m_num = re.match(r"^(\d{1,2})[\.)]\s+\S", stripped)
        m_part = _PART_HEADER_RE.match(stripped)
        if m_num:
            starts.append((int(m_num.group(1)), i))
        elif m_part:
            starts.append((int(m_part.group(1)), i))

    if len(starts) < 2:
        return [text]

    by_num: dict[int, int] = {}
    for num, idx in starts:
        if num not in by_num:
            by_num[num] = idx

    ordered = sorted(by_num.items(), key=lambda x: x[0])
    parts: list[str] = []
    for j, (_, start_i) in enumerate(ordered):
        end_i = ordered[j + 1][1] if j + 1 < len(ordered) else len(lines)
        chunk = "\n".join(lines[start_i:end_i]).strip()
        if chunk:
            parts.append(chunk)
    return parts or [text]


def part_word_targets(plan_parts: list[str], *, total: int = TARGET_TOTAL_WORDS) -> list[int]:
    """Целевой объём слов на часть — по % из плана или поровну."""
    n = len(plan_parts)
    if n == 0:
        return []

    pcts: list[float | None] = []
    for spec in plan_parts:
        m = _PCT_RE.search(spec)
        pcts.append(float(m.group(1)) if m else None)

    if all(p is not None for p in pcts):
        s = sum(p or 0.0 for p in pcts)
        if 85 <= s <= 115:
            weights = [(p or 0.0) / s for p in pcts]
        else:
            weights = [1.0 / n] * n
    else:
        weights = [1.0 / n] * n

    raw = [max(WORDS_PER_PART_MIN, int(total * w)) for w in weights]
    # Подогнать сумму к total
    diff = total - sum(raw)
    if raw:
        raw[-1] = max(WORDS_PER_PART_MIN, raw[-1] + diff)
    return [min(WORDS_PER_PART_MAX, max(WORDS_PER_PART_MIN, t)) for t in raw]


def narrative_arc_rules(part_index: int, part_total: int) -> str:
    """Фаза драматургии: что можно / нельзя в этой части."""
    if part_total <= 1:
        phase = "финал"
    elif part_index == 1:
        phase = "завязка"
    elif part_index == part_total:
        phase = "финал"
    elif part_index == part_total - 1:
        phase = "предфинал"
    elif part_index <= max(2, part_total // 3):
        phase = "ранняя эскалация"
    else:
        phase = "осложнения"

    rules: dict[str, str] = {
        "завязка": (
            "Фаза: ЗАВЯЗКА.\n"
            "Покажи удар хука и первую реакцию героини. "
            "Введи ставки (что потеряет / что хочет вернуть).\n"
            "ЗАПРЕЩЕНО: примирение с антагонистом, прощение «навсегда», "
            "моральный итог всей истории, эпилог, «новая жизнь», "
            "полное разоблачение виновных."
        ),
        "ранняя эскалация": (
            "Фаза: РАННЯЯ ЭСКАЛАЦИЯ.\n"
            "Добавь НОВОЕ осложнение: новая ложь, унижение, документ, "
            "свидетель, предательство союзника.\n"
            "Конфликт должен стать ОСТРЕЕ, чем в начале.\n"
            "ЗАПРЕЩЕНО: закрывать главный конфликт, писать «она простила, "
            "но больше не доверяет» (это заготовка финала), "
            "подводить итог всей истории, эпилог."
        ),
        "осложнения": (
            "Фаза: ОСЛОЖНЕНИЯ И НАРАСТАНИЕ.\n"
            "Каждая сцена — новый поворот, а не перефраз старого. "
            "Героиня может колебаться, но не приходит к окончательному решению.\n"
            "ЗАПРЕЩЕНО: повторять одну эмоцию в разных сценах "
            "(прощение без доверия, тихое примирение, мораль «жить дальше»). "
            "ЗАПРЕЩЕНО: публичное разоблачение, суд, торжество справедливости, "
            "эпилог — это только для последней части."
        ),
        "предфинал": (
            "Фаза: ПРЕДФИНАЛ / ПИК НАПРЯЖЕНИЯ.\n"
            "Доведи конфликт до точки невозврата: всё на кону, "
            "виновные ещё не наказаны, героиня на грани поражения.\n"
            "ЗАПРЕЩЕНО: полная развязка, эпилог, «новая жизнь», "
            "окончательное прощение. Можно оборвать на пике или на пороге решающей сцены."
        ),
        "финал": (
            "Фаза: ФИНАЛ (единственная развязка за весь рассказ).\n"
            "ОБЯЗАТЕЛЬНО: одна кульминация → справедливость → последствия для виновных → "
            "короткий эпилог.\n"
            "НЕ повторяй промежуточные «почти-финалы» из предыдущих частей. "
            "Не пиши вторую и третью развязку подряд — только одна."
        ),
    }
    return rules.get(phase, rules["осложнения"])


def build_story_part_prompt(
    *,
    hook: str,
    part_spec: str,
    part_index: int,
    part_total: int,
    target_words: int,
    prev_tail: str,
    is_first: bool,
    is_last: bool,
) -> str:
    arc = narrative_arc_rules(part_index, part_total)
    intro = (
        "Отлично. Запомни план, отталкивайся от него при написании текста рассказа.\n"
        "Сделаем рассказ по СВЯЗАННЫМ МЕЖДУ СОБОЙ частям — один сквозной конфликт, "
        "без нескольких концовок.\n"
        "Цифры и числа — буквами. Только русские буквы и слова. "
        "Без md, без заголовков — сразу на озвучку.\n\n"
        if is_first
        else ""
    )
    hook_rule = (
        f"Начни рассказ с этого хука (первая сцена):\n{hook.strip()}\n"
        "Не пиши вступление до хука.\n\n"
        if is_first
        else (
            "Продолжай с места остановки предыдущей части. "
            "Не пересказывай уже написанное. "
            "Не дублируй сцены и эмоциональные итоги из хвоста.\n\n"
        )
    )
    finish_rule = (
        f"{arc}\n\n"
        if not is_last
        else (
            f"{arc}\n"
            "Заверши сюжет полностью в этой части. Не обрывай на полуслове.\n\n"
        )
    )
    tail_block = (
        f"Хвост уже написанного (продолжай отсюда):\n{prev_tail}\n\n"
        if prev_tail.strip()
        else ""
    )
    return (
        f"{intro}"
        f"Сейчас пишем часть {part_index}/{part_total}.\n"
        f"Объём этой части: примерно {target_words} слов (±15%).\n"
        f"{hook_rule}"
        f"{finish_rule}"
        f"План этой части:\n{part_spec.strip()}\n\n"
        f"{tail_block}"
        "Пиши только художественный текст этой части, сплошным текстом."
    )


def build_story_continue_prompt(
    *,
    plan: str,
    story_so_far: str,
    words_needed: int,
    has_finale: bool = False,
) -> str:
    """Короткий добор объёма — не больше CONTINUE_CHUNK_WORDS за вызов."""
    chunk = min(words_needed, CONTINUE_CHUNK_WORDS)
    tail = _tail(story_so_far, max_chars=3500)
    finale_rule = (
        "В хвосте уже есть развязка/эпилог — НЕ пиши второй финал. "
        "Расширь сцену ПЕРЕД развязкой: детали, диалоги, напряжение. "
        "Не дублируй мораль «простила, но не доверяет».\n"
        if has_finale
        else (
            "Развязки ещё не было — не пиши финал, эпилог и торжество справедливости. "
            "Добавь новое осложнение или углуби текущий конфликт.\n"
        )
    )
    return (
        "Продолжай тот же рассказ с места остановки. Не пересказывай уже написанное.\n"
        f"Напиши ещё примерно {chunk} слов.\n"
        f"{finale_rule}"
        "Цифры — буквами. Только русский художественный текст, без заголовков.\n"
        "Не повторяй одну и ту же мысль разными словами.\n\n"
        f"План (ориентир):\n{_tail(plan, max_chars=4000)}\n\n"
        f"Конец уже написанного:\n{tail}\n\n"
        "Продолжай сразу с следующего предложения."
    )


_FINALE_MARKERS_RE = re.compile(
    r"(?i)"
    r"(новая жизнь|эпилог|торжеств\w+\s+справедлив|"
    r"виновн\w+\s+получил\w*|она\s+простил\w+.*но\s+больше\s+не\s+доверя"
    r"|спокойн\w+\s+утр|счастлив\w+\s+конец|наконец\s+всё\s+закончил"
    r"|справедливость\s+восторжеств)"
)


@dataclass
class HookStoryResult:
    text: str
    meta: StoryMeta
    hook: str
    word_count: int
    parts: list[str]
    plan: str = ""
    canon: str = ""


def save_story_plan(plan: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((plan or "").strip() + "\n", encoding="utf-8")
    return path


def generate_hook_plan(
    client: OpenAIRewriter,
    *,
    hook: str,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
    progress_start: float = 0.05,
    progress_end: float = 0.25,
) -> str:
    """Шаг 1: 9–12 пропорциональных частей сценария."""

    def progress(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(pct, msg)
        else:
            log(msg)

    hook_clean = (hook or "").strip()
    user = build_plan_prompt(hook_clean)
    progress(progress_start, "Строю план по частям (9–12)…")
    log(f"hook-plan: user_chars={len(user)}")

    plan = client.complete(
        label="hook-plan",
        system=PLAN_SYSTEM,
        user=user,
    )
    plan = (plan or "").strip()
    if not plan:
        raise RuntimeError("Пустой план по хуку")
    log(f"hook-plan: out_chars={len(plan)}")
    progress(progress_end, "План готов, пишу рассказ…")
    return plan


def generate_hook_story_text(
    client: OpenAIRewriter,
    *,
    hook: str,
    plan: str,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
    progress_start: float = 0.25,
    progress_end: float = 0.85,
) -> tuple[str, list[str]]:
    """Шаг 2: рассказ по частям плана (~700–900 слов на вызов)."""

    def progress(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(pct, msg)
        else:
            log(msg)

    hook_clean = (hook or "").strip()
    plan_parts = parse_plan_parts(plan)
    if not plan_parts:
        raise RuntimeError("Не удалось разобрать части плана")

    targets = part_word_targets(plan_parts)
    n = len(plan_parts)
    log(f"hook-story: частей плана={n}, targets={targets}")

    written: list[str] = []
    span = progress_end - progress_start

    for i, (spec, target) in enumerate(zip(plan_parts, targets, strict=True), start=1):
        pct = progress_start + span * ((i - 1) / n)
        progress(pct, f"Пишу часть {i}/{n} (~{target} слов)…")

        prev_tail = _tail(written[-1], max_chars=3500) if written else ""
        user = build_story_part_prompt(
            hook=hook_clean,
            part_spec=spec,
            part_index=i,
            part_total=n,
            target_words=target,
            prev_tail=prev_tail,
            is_first=i == 1,
            is_last=i == n,
        )
        log(f"hook-part-{i}/{n}: user_chars={len(user)}")

        raw = client.complete(
            label=f"hook-part-{i}/{n}",
            system=STORY_SYSTEM,
            user=user,
        )
        chunk = _strip_part_chrome(raw)
        if not chunk.strip():
            raise RuntimeError(f"Пустая часть рассказа #{i}")
        written.append(chunk.strip())
        total = word_count("\n\n".join(written))
        log(f"hook-part-{i}/{n}: ~{word_count(chunk)} слов, всего ~{total}")

    story = "\n\n".join(written).strip()
    wc = word_count(story)

    # Короткие доборы, если суммарно не дотянули (не один гигантский запрос).
    cont_i = 0
    while wc < TARGET_MIN_WORDS and cont_i < MAX_CONTINUE_CHUNKS:
        need = TARGET_MIN_WORDS - wc
        cont_i += 1
        chunk_need = min(need, CONTINUE_CHUNK_WORDS)
        progress(
            progress_end - 0.05,
            f"Добираю объём: кусок {cont_i} (~{chunk_need} слов)…",
        )
        cont_user = build_story_continue_prompt(
            plan=plan,
            story_so_far=story,
            words_needed=chunk_need,
            has_finale=bool(_FINALE_MARKERS_RE.search(_tail(story, max_chars=2000))),
        )
        try:
            cont_raw = client.complete(
                label=f"hook-story-continue-{cont_i}",
                system=STORY_SYSTEM,
                user=cont_user,
            )
            cont = _strip_part_chrome(cont_raw)
            if not cont.strip():
                break
            story = f"{story.rstrip()}\n\n{cont.strip()}"
            written.append(cont.strip())
            wc = word_count(story)
            log(f"hook-story-continue-{cont_i}: всего ~{wc} слов")
        except Exception as exc:
            log(f"hook-story-continue-{cont_i}: не удалось — {exc}")
            break

    progress(progress_end, f"Рассказ готов (~{word_count(story)} слов)")
    return story, written


def generate_hook_story(
    *,
    hook: str,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    prefix: str = "",
    on_progress: ProgressCb | None = None,
    prompt_path: Path | None = None,  # noqa: ARG001 — legacy, не используется
    cancel: CancelToken | None = None,
) -> HookStoryResult:
    """План по хуку → рассказ по частям → мета."""

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

    log(f"Хук: {hook_clean[:120]}{'…' if len(hook_clean) > 120 else ''}")
    progress(0.02, "Подключаюсь к GPT…")
    client = OpenAIRewriter(api_key=api_key, model=model, base_url=base_url)
    if cancel:
        cancel.register(client)

    try:
        plan = generate_hook_plan(
            client,
            hook=hook_clean,
            on_progress=on_progress,
            cancel=cancel,
            progress_start=0.05,
            progress_end=0.25,
        )

        story_body, story_parts = generate_hook_story_text(
            client,
            hook=hook_clean,
            plan=plan,
            on_progress=on_progress,
            cancel=cancel,
            progress_start=0.25,
            progress_end=0.85,
        )

        progress(0.88, "Собираю мета для YouTube…")
        meta = StoryMeta()
        try:
            meta_raw = client.complete(
                label="hook-meta",
                system=(
                    "Ты маркетолог YouTube-аудиорассказов (семейная мелодрама). "
                    "Выдай только нумерованный мета-блок 1–6, без текста рассказа."
                ),
                user=(
                    f"Хук: {hook_clean}\n\n"
                    f"План:\n{_tail(plan, max_chars=2500)}\n\n"
                    f"Начало рассказа:\n{_tail(story_body[:2000], max_chars=800)}\n\n"
                    f"Конец рассказа:\n{_tail(story_body, max_chars=800)}\n\n"
                    "Сформируй:\n"
                    "1. Пять коротких цепляющих названий длиной 2–4 слова.\n"
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
        except Exception as exc:
            if is_content_policy_error(exc):
                log("hook-meta: content_policy — мета пустая, рассказ сохранён")
            else:
                raise

        body_only, maybe_meta = parse_story_and_meta(story_body)
        if not maybe_meta.is_empty() and meta.is_empty():
            meta = maybe_meta
        if body_only.strip():
            story_body = body_only

        final = f"{prefix}{story_body}" if prefix else story_body
        wc = word_count(story_body)
        progress(1.0, f"Рассказ готов (~{wc} слов)")
        if wc < TARGET_MIN_WORDS:
            log(
                f"Внимание: слов ~{wc} < целевых {TARGET_MIN_WORDS} "
                f"(частей {len(story_parts)}, доборов ограничен)"
            )

        return HookStoryResult(
            text=final,
            meta=meta,
            hook=hook_clean,
            word_count=wc,
            parts=story_parts,
            plan=plan,
            canon="",
        )
    finally:
        client.close()
