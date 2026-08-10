"""Рассказ с нуля по профессии: слоты, промпт, чанки, мета."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rewriter.cancel import CancelToken
from rewriter.logutil import log
from rewriter.openai_client import DEFAULT_BASE_URL, OpenAIRewriter

ProgressCb = Callable[[float, str], None]

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "profession_story_v1.txt"

TARGET_MIN_WORDS = 10_000
TARGET_MAX_WORDS = 12_000
MAX_STORY_PARTS = 10
WORDS_PER_PART = 2_200

PLACES = [
    "больница",
    "школа",
    "ресторан",
    "санаторий",
    "фабрика",
    "пансионат",
    "театр",
    "гостиница",
    "вокзал",
    "детский дом",
    "дом престарелых",
    "сельская администрация",
    "музей",
    "библиотека",
    "ателье",
    "поликлиника",
    "интернат",
    "суд",
    "аптека",
    "типография",
]

DREAMS = [
    "выйти замуж",
    "вернуть семью",
    "стать матерью",
    "восстановиться в профессии",
    "заслужить уважение детей",
    "вернуть доброе имя",
    "получить признание",
    "спасти близкого человека",
]

IDEALIZED = [
    "мужчина",
    "муж",
    "начальник",
    "взрослый ребёнок",
    "подруга",
    "родственник",
    "известный специалист",
    "благодетель",
]

UNDERVALUED = [
    "скромный коллега",
    "неприметный сосед",
    "бывший одноклассник",
    "пожилая женщина",
    "неловкий врач",
    "уборщик",
    "водитель",
    "сторож",
    "социальный работник",
]

CONFLICTS = [
    "предательство",
    "измена",
    "несправедливое увольнение",
    "ложное обвинение",
    "борьба за наследство",
    "профессиональная зависть",
    "семейная тайна",
    "исчезновение человека",
    "подстава",
]

INCIDENTS = [
    "падение человека",
    "отравление",
    "пожар",
    "исчезновение ребёнка",
    "смерть пациента",
    "кража денег",
    "подмена документов",
    "авария",
    "покушение",
    "пропажа ценностей",
]

ENDINGS = [
    "романтический",
    "семейный",
    "профессиональный",
    "справедливый",
    "горько-сладкий",
    "открытый, но обнадёживающий",
]

ANTAGONIST_HINTS = [
    "завистливая коллега, которая выглядит жертвой",
    "родственник, притворяющийся опекуном",
    "начальник с безупречной репутацией",
    "бывший супруг, играющий в раскаяние",
    "подруга детства с тайным мотивом",
    "врач или эксперт, которому все доверяют",
    "сын или дочь, прикрывающиеся любовью",
    "благодетель, скупающий чужую зависимость",
]

FEMALE_NAMES = [
    "Галина",
    "Валентина",
    "Нина",
    "Тамара",
    "Людмила",
    "Зинаида",
    "Раиса",
    "Вера",
    "Надежда",
    "Любовь",
    "Светлана",
    "Ирина",
    "Ольга",
    "Татьяна",
    "Елена",
    "Наталья",
    "Марина",
    "Лариса",
    "Антонина",
    "Клавдия",
    "Алевтина",
    "Инна",
    "Эльвира",
    "Жанна",
]


@dataclass
class StorySlots:
    name: str
    age: int
    profession: str
    place: str
    dream: str
    idealized: str
    undervalued: str
    conflict: str
    incident: str
    antagonist: str
    ending: str

    def heroine_line(self) -> str:
        return f"{self.name}, {self.age} лет, {self.profession}"

    def as_dict(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "age": self.age,
            "profession": self.profession,
            "place": self.place,
            "dream": self.dream,
            "idealized": self.idealized,
            "undervalued": self.undervalued,
            "conflict": self.conflict,
            "incident": self.incident,
            "antagonist": self.antagonist,
            "ending": self.ending,
        }


@dataclass
class StoryMeta:
    titles: list[str] = field(default_factory=list)
    yt_titles: list[str] = field(default_factory=list)
    description: str = ""
    preview_phrases: list[str] = field(default_factory=list)
    summary: str = ""
    plot_turns: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.titles,
                self.yt_titles,
                self.description.strip(),
                self.preview_phrases,
                self.summary.strip(),
                self.plot_turns,
            ]
        )

    def format_for_ui(self) -> str:
        blocks: list[str] = []
        if self.titles:
            blocks.append("Названия:\n" + "\n".join(f"• {t}" for t in self.titles))
        if self.yt_titles:
            blocks.append(
                "YouTube-заголовки:\n" + "\n".join(f"• {t}" for t in self.yt_titles)
            )
        if self.description.strip():
            blocks.append("Описание:\n" + self.description.strip())
        if self.preview_phrases:
            blocks.append(
                "Превью (≤4 слов):\n"
                + "\n".join(f"• {t}" for t in self.preview_phrases)
            )
        if self.summary.strip():
            blocks.append("Краткое содержание:\n" + self.summary.strip())
        if self.plot_turns:
            blocks.append(
                "Сюжетные повороты:\n"
                + "\n".join(f"• {t}" for t in self.plot_turns)
            )
        return "\n\n".join(blocks)


@dataclass
class ProfessionStoryResult:
    text: str
    meta: StoryMeta
    slots: StorySlots
    word_count: int
    parts: list[str]
    outline: str = ""
    bible: str = ""


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def load_master_prompt(path: Path | None = None) -> str:
    p = path or PROMPT_PATH
    return p.read_text(encoding="utf-8").strip()


def pick_slots(profession: str, *, rng: random.Random | None = None) -> StorySlots:
    r = rng or random.Random()
    prof = (profession or "").strip()
    if not prof:
        raise ValueError("Профессия пустая")
    return StorySlots(
        name=r.choice(FEMALE_NAMES),
        age=r.randint(45, 65),
        profession=prof,
        place=r.choice(PLACES),
        dream=r.choice(DREAMS),
        idealized=r.choice(IDEALIZED),
        undervalued=r.choice(UNDERVALUED),
        conflict=r.choice(CONFLICTS),
        incident=r.choice(INCIDENTS),
        antagonist=(
            "придумать самостоятельно; подсказка роли: " + r.choice(ANTAGONIST_HINTS)
        ),
        ending=r.choice(ENDINGS),
    )


def _filled_data_block(slots: StorySlots) -> str:
    return (
        "## ИСХОДНЫЕ ДАННЫЕ\n\n"
        f"Главная героиня: {slots.heroine_line()}.\n\n"
        f"Основное место действия: {slots.place}.\n\n"
        f"Главная болезненная мечта героини: {slots.dream}.\n\n"
        f"Человек, которого героиня ошибочно идеализирует: {slots.idealized}.\n\n"
        f"Человек, которого героиня сначала недооценивает: {slots.undervalued}.\n\n"
        f"Основной конфликт: {slots.conflict}.\n\n"
        f"Центральное происшествие: {slots.incident}.\n\n"
        f"Настоящий виновник или скрытый антагонист: {slots.antagonist}.\n\n"
        f"Желаемый финал: {slots.ending}.\n\n"
        f"Желаемая длина: {TARGET_MIN_WORDS}–{TARGET_MAX_WORDS} слов "
        f"(строго в этом диапазоне).\n\n"
        "Все исходные данные выше уже заданы — используй их как есть, "
        "не заменяй профессию и не меняй выбранные слоты.\n"
    )


def fill_master_prompt(template: str, slots: StorySlots) -> str:
    """Подставить заполненные ИСХОДНЫЕ ДАННЫЕ в мастер-промпт."""
    data = _filled_data_block(slots)
    pattern = re.compile(
        r"## ИСХОДНЫЕ ДАННЫЕ\n.*?^(?=---\n)",
        re.MULTILINE | re.DOTALL,
    )
    if pattern.search(template):
        return pattern.sub(data + "\n", template, count=1)
    return data + "\n\n" + template


def _strip_part_chrome(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(
        r"^(?:часть\s*\d+|продолжение|глава\s*\d+)[^\n]*\n+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return t.strip()


_META_START_RE = re.compile(
    r"(?im)^(?:"
    r"(?:\d+[\).]\s*)?(?:пять\s+)?(?:кликбейтн\w*\s+)?назван"
    r"|названи[яе]\s*(?:ролика|рассказа|для\s+youtube)?"
    r"|youtube[- ]?заголов"
    r"|заголовк\w*\s+для\s+youtube"
    r"|кратк\w*\s+эмоциональн\w*\s+описан"
    r"|описани[ея]\s+ролика"
    r"|вариант\w*\s+текста\s+для\s+превью"
    r"|текст\w*\s+для\s+превью"
    r"|кратк\w*\s+содержан"
    r"|сюжетн\w*\s+поворот"
    r"|маркетинг"
    r"|после\s+окончания\s+рассказа"
    r")"
)

def parse_story_and_meta(raw: str) -> tuple[str, StoryMeta]:
    """Отделить тело рассказа от маркетингового блока."""
    text = (raw or "").strip()
    if not text:
        return "", StoryMeta()

    # Явный разделитель
    for sep in (
        "\n---META---\n",
        "\n===META===\n",
        "\n# МАРКЕТИНГ\n",
        "\n## МАРКЕТИНГ\n",
    ):
        if sep in text:
            story, meta_raw = text.split(sep, 1)
            return story.strip(), parse_meta_block(meta_raw)

    # Нумерованный блок 1.…назван… ближе к концу ответа
    numbered = list(
        re.finditer(
            r"(?im)^1[\).]\s*(?:пять\s+)?(?:кликбейтн\w*\s+)?назван",
            text,
        )
    )
    if numbered:
        m = numbered[-1]
        if m.start() >= 20:
            story = text[: m.start()].strip()
            meta = parse_meta_block(text[m.start() :])
            if not meta.is_empty():
                return story, meta

    match = _META_START_RE.search(text)
    if match and match.start() >= 20:
        story = text[: match.start()].strip()
        meta = parse_meta_block(text[match.start() :])
        if not meta.is_empty() and word_count(story) >= 30:
            return story, meta

    return text, StoryMeta()


def parse_meta_block(raw: str) -> StoryMeta:
    meta = StoryMeta()
    text = (raw or "").strip()
    if not text:
        return meta

    # Разбить на секции по нумерации 1)…6) или заголовкам
    parts = re.split(r"(?im)^(?:#{1,3}\s*)?(\d+)[\).]\s*", text)
    # parts: [preamble, num, body, num, body, ...]
    sections: dict[int, str] = {}
    if len(parts) >= 3:
        for i in range(1, len(parts) - 1, 2):
            try:
                n = int(parts[i])
            except ValueError:
                continue
            sections[n] = parts[i + 1].strip()

    def lines_from(block: str) -> list[str]:
        out: list[str] = []
        for line in block.splitlines():
            s = line.strip()
            if not s:
                continue
            s = re.sub(r"^[-•*]\s*", "", s)
            s = re.sub(r"^\d+[\).]\s*", "", s)
            out.append(s)
        # первая строка часто заголовок секции («Названия», «Превью»…)
        if out and re.match(
            r"(?i)^(пять\s+)?(кликбейтн\w*\s+)?"
            r"(назван\w*|заголов\w*|описан\w*|превью\w*|"
            r"содержан\w*|поворот\w*|youtube\w*)\b.{0,40}$",
            out[0],
        ):
            out = out[1:]
        return out

    if sections:
        meta.titles = lines_from(sections.get(1, ""))[:5]
        meta.yt_titles = lines_from(sections.get(2, ""))[:5]
        desc_lines = lines_from(sections.get(3, ""))
        meta.description = " ".join(desc_lines).strip()
        meta.preview_phrases = lines_from(sections.get(4, ""))[:5]
        sum_lines = lines_from(sections.get(5, ""))
        meta.summary = " ".join(sum_lines).strip()
        meta.plot_turns = lines_from(sections.get(6, ""))
        return meta

    # fallback: по ключевым словам в заголовках
    buckets: dict[str, list[str]] = {
        "titles": [],
        "yt": [],
        "desc": [],
        "preview": [],
        "summary": [],
        "turns": [],
    }
    current = ""
    for line in text.splitlines():
        bare = line.strip()
        if not bare:
            continue
        low = bare.lower()
        header = False
        if re.match(r"(?i)^(?:#{1,3}\s*)?(?:\d+[\).]\s*)?", bare):
            if "youtube" in low or "заголов" in low:
                current = "yt"
                header = True
            elif "назван" in low:
                current = "titles"
                header = True
            elif "описан" in low:
                current = "desc"
                header = True
            elif "превью" in low:
                current = "preview"
                header = True
            elif "содержан" in low:
                current = "summary"
                header = True
            elif "поворот" in low:
                current = "turns"
                header = True
        if header:
            continue
        if current and current in buckets:
            s = re.sub(r"^[-•*]\s*", "", bare)
            s = re.sub(r"^\d+[\).]\s*", "", s)
            buckets[current].append(s)

    meta.titles = buckets["titles"][:5]
    meta.yt_titles = buckets["yt"][:5]
    meta.description = " ".join(buckets["desc"]).strip()
    meta.preview_phrases = buckets["preview"][:5]
    meta.summary = " ".join(buckets["summary"]).strip()
    meta.plot_turns = buckets["turns"]
    return meta


def _tail(text: str, *, max_chars: int = 1800) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    return t[-max_chars:]


def generate_profession_story(
    *,
    profession: str,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    prefix: str = "",
    on_progress: ProgressCb | None = None,
    rng: random.Random | None = None,
    prompt_path: Path | None = None,
    cancel: CancelToken | None = None,
) -> ProfessionStoryResult:
    """План → библия → части рассказа → мета. В text только тело (+prefix)."""

    def progress(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(pct, msg)
        else:
            log(msg)

    slots = pick_slots(profession, rng=rng)
    template = load_master_prompt(prompt_path)
    master = fill_master_prompt(template, slots)
    log(
        "Слоты: "
        f"{slots.heroine_line()}; место={slots.place}; "
        f"мечта={slots.dream}; конфликт={slots.conflict}"
    )

    progress(0.02, "Подключаюсь к GPT…")
    client = OpenAIRewriter(api_key=api_key, model=model, base_url=base_url)
    if cancel:
        cancel.register(client)

    try:
        progress(0.05, "Строю план рассказа…")
        outline = client.complete(
            label="profession-outline",
            system=(
                "Ты — сценарист длинных жизненных аудиорассказов. "
                "Отвечай только содержимым плана, без рассказа целиком."
            ),
            user=(
                f"{master}\n\n"
                "Сейчас НЕ пиши рассказ. Составь подробный внутренний план "
                f"на {TARGET_MIN_WORDS}–{TARGET_MAX_WORDS} слов итогового текста:\n"
                "— хук (первая фраза);\n"
                "— акты и ключевые сцены (8–12 пунктов);\n"
                "— тайны и ложные следы;\n"
                "— роль идеализируемого и недооценённого человека;\n"
                "— разоблачение антагониста;\n"
                "— тип финала и эмоциональный катарсис.\n"
                "Пиши план по-русски, компактно, 600–1200 слов."
            ),
        )

        progress(0.12, "Собираю библию персонажей…")
        bible = client.complete(
            label="profession-bible",
            system=(
                "Ты ведёшь continuity bible для длинного рассказа. "
                "Только справочник, без художественного текста рассказа."
            ),
            user=(
                f"Исходные данные: {slots.heroine_line()}; место={slots.place}; "
                f"мечта={slots.dream}; идеализирует={slots.idealized}; "
                f"недооценивает={slots.undervalued}; конфликт={slots.conflict}; "
                f"инцидент={slots.incident}; антагонист={slots.antagonist}; "
                f"финал={slots.ending}.\n\n"
                f"План:\n{outline}\n\n"
                "Составь библию: имена/возраст/роли всех важных персонажей, "
                "отношения, ключевые факты, запреты на смену имён. "
                "До 800 слов."
            ),
        )

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
                "Это ПОСЛЕДНЯЯ часть: логично заверши сюжет выбранным типом финала, "
                "дай катарсис. Не обрывай на полуслове."
                if must_finish
                else (
                    "Это НЕ финал: закончи часть на напряжённом, но естественном "
                    "переходе к следующей сцене. Не раскрывай антагониста до конца "
                    "и не пиши эпилог."
                )
            )
            hook_rule = (
                "Начни с жёсткого сюжетного хука по правилам мастер-промпта "
                "(первая фраза — удар, не атмосфера)."
                if is_first
                else (
                    "Продолжай сразу с места остановки предыдущей части. "
                    "Не пересказывай уже написанное, не начинай заново."
                )
            )

            user = (
                f"Мастер-бриф (соблюдай):\n{master}\n\n"
                f"План:\n{outline}\n\n"
                f"Библия персонажей:\n{bible}\n\n"
                f"Краткое содержание уже написанного:\n"
                f"{running_summary or '(пока ничего)'}\n\n"
                f"Хвост предыдущей части:\n{prev_tail or '(это первая часть)'}\n\n"
                f"Задача: напиши часть {part_i} рассказа объёмом примерно "
                f"{target_now} слов (допустимо ±15%).\n"
                f"{hook_rule}\n"
                f"{finish_rule}\n"
                "Пиши только художественный текст этой части. "
                "Без заголовков частей, без мета-блока, без названий и описаний "
                "для YouTube, без списков поворотов."
            )

            chunk = client.complete(
                label=f"profession-part-{part_i}",
                system=(
                    "Ты пишешь фрагмент длинного жизненного аудиорассказа. "
                    "В ответе только текст фрагмента."
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
                label=f"profession-summary-{part_i}",
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
            label="profession-meta",
            system=(
                "Ты маркетолог YouTube-аудиорассказов. "
                "Выдай только нумерованный мета-блок 1–6, без текста рассказа."
            ),
            user=(
                f"Героиня: {slots.heroine_line()}. Место: {slots.place}. "
                f"Конфликт: {slots.conflict}. Финал: {slots.ending}.\n\n"
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
        # на случай если модель всё же вставила рассказ+мета в какую-то часть
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

        return ProfessionStoryResult(
            text=final,
            meta=meta,
            slots=slots,
            word_count=wc,
            parts=parts,
            outline=outline,
            bible=bible,
        )
    finally:
        client.close()
