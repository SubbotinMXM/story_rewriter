"""Мастер-промпты превью: текст (опц.) + картинка (обяз.). Правки — вкладка «Промпты»."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ASSETS = _ROOT / "assets"
_PROMPTS = Path(__file__).resolve().parent / "prompts"
_USER_DIR = _ROOT / "user_presets"
_USER_INDEX = _USER_DIR / "index.json"

_PLACEHOLDERS = ("thumb_text", "story_text", "variant_hint")

_VARIANT_HINTS = {
    1: "ракурс чуть ближе к лицу героя, козырь максимально читаем",
    2: "чуть шире кадр, сильнее реакция антагониста на среднем плане",
    3: "акцент на предмете-доказательстве на переднем плане, герой по грудь",
}


def _safe_template_format(template: str, **kwargs: str) -> str:
    """Подставляет только {thumb_text}/{story_text}/{variant_hint}, остальные { } не трогает."""
    out = template
    for key in _PLACEHOLDERS:
        out = out.replace("{" + key + "}", kwargs.get(key, ""))
    return out


def _ensure_placeholders(template: str, *, include_thumb_text: bool = True) -> str:
    t = template.rstrip() + "\n"
    if include_thumb_text and "{thumb_text}" not in t:
        t += (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "УТВЕРЖДЁННАЯ ФРАЗА ДЛЯ ПРЕВЬЮ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "{thumb_text}\n"
        )
    if "{story_text}" not in t:
        t += (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "РАССКАЗ ДЛЯ АНАЛИЗА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "{story_text}\n"
        )
    if "{variant_hint}" not in t:
        t += "\nВариант кадра: {variant_hint}\n"
    return t


@dataclass(frozen=True)
class ThumbnailPreset:
    """Мастер-промпт: опц. text + опц. image_prep (GPT→промпт) + обяз. image."""

    id: str
    name: str
    description: str
    example_image: Path
    image_prompt_file: Path | None = None
    image_prompt_template: str | None = None
    text_prompt_file: Path | None = None
    text_prompt_template: str | None = None
    image_prep_prompt_file: Path | None = None
    image_prep_prompt_template: str | None = None
    builtin: bool = False

    def raw_text_template(self) -> str:
        if self.text_prompt_template is not None:
            return self.text_prompt_template
        if self.text_prompt_file and self.text_prompt_file.is_file():
            return self.text_prompt_file.read_text(encoding="utf-8")
        return ""

    def raw_image_prep_template(self) -> str:
        if self.image_prep_prompt_template is not None:
            return self.image_prep_prompt_template
        if self.image_prep_prompt_file and self.image_prep_prompt_file.is_file():
            return self.image_prep_prompt_file.read_text(encoding="utf-8")
        return ""

    def needs_text_prompt(self) -> bool:
        return bool(self.raw_text_template().strip())

    def needs_image_prep(self) -> bool:
        """GPT сначала собирает image-промпт (случайная внешность и т.п.)."""
        return bool(self.raw_image_prep_template().strip())

    def needs_story_input(self) -> bool:
        """Нужен ли .txt рассказа для превью.

        False при каталоге titles.txt или когда GPT-фразы из рассказа не нужны.
        """
        from rewriter.preset_titles import has_title_catalog

        if has_title_catalog(self.id):
            return False
        return self.needs_text_prompt()

    def needs_gpt(self) -> bool:
        return self.needs_text_prompt() or self.needs_image_prep()

    def text_prompt(self, story_text: str) -> str:
        return self.text_prompt_multi(story_text, count=1)

    def text_prompt_multi(self, story_text: str, *, count: int = 3) -> str:
        rules = self.raw_text_template().strip()
        if not rules:
            raise ValueError("У мастер-промпта нет текстового промпта")
        n = max(1, min(int(count), 3))
        if n > 1:
            diversity = (
                f"Сформулируй ровно {n} РАЗНЫХ варианта по ОДНОМУ рассказу.\n"
                "Варианты должны заметно отличаться (не одна конструкция с заменой слова).\n"
                f"Формат ответа СТРОГО {n} строки: по ОДНОМУ варианту на строку, "
                "без нумерации, без пояснений, без пустых строк.\n"
                "Внутри варианта не делай переносов строк — только пробелы "
                "(разбивку на строки для картинки сделает image-промпт)."
            )
        else:
            diversity = (
                "Сформулируй ровно ОДИН вариант.\n"
                "Формат ответа: только эта фраза в одну строку, без пояснений "
                "и без внутренних переносов строк."
            )
        # Полный мастер-промпт с плейсхолдерами (как у №2)
        if "{story_text}" in rules:
            body = rules
            if "{count_rules}" in body:
                body = body.replace("{count_rules}", diversity)
            else:
                body = f"{body.rstrip()}\n\n{diversity}"
            return body.replace("{story_text}", story_text)
        # Legacy (мастер №1): правила + diversity + рассказ
        return (
            "Ты — сценарный редактор вирусных YouTube-превью для драматических "
            "житейских историй.\n"
            "Проанализируй ПОЛНЫЙ рассказ ниже и создай цепляющий текст для превью.\n\n"
            f"{rules}\n\n"
            f"{diversity}\n\n"
            "РАССКАЗ (полный текст):\n"
            f"{story_text}"
        )

    def repair_prompt(self, phrase: str, errors: list[str]) -> str:
        rules = self.raw_text_template().strip()
        return (
            "Исправь фразу для YouTube-превью по правилам ниже.\n"
            "Сохрани смысл: конфликт (часть 1) + поворотный обрыв (часть 2).\n"
            "Строго 10–24 слов, ПРОПИСНЫЕ, кириллица; роли вместо пустых имён.\n"
            "ОБЯЗАТЕЛЬНО закончи фразу многоточием «...» — обрыв на полуслове, "
            "без досказанного финала.\n"
            "Исправь проблемы:\n"
            + "\n".join(f"— {e}" for e in errors)
            + "\n\n"
            + rules
            + "\n\nИсходная фраза:\n"
            + phrase
            + "\n\nВерни ТОЛЬКО исправленную фразу."
        )

    def repair_prompt_multi(
        self, items: list[tuple[int, str, list[str]]]
    ) -> str:
        rules = self.raw_text_template().strip()
        blocks = []
        for num, phrase, errors in items:
            blocks.append(
                f"#{num}\nФраза: {phrase}\nПроблемы:\n"
                + "\n".join(f"— {e}" for e in errors)
            )
        return (
            "Исправь фразы для YouTube-превью.\n"
            "Каждая: 10–24 слов, ПРОПИСНЫЕ, конфликт + поворотный обрыв с «...».\n"
            "Часть 2 стыкуется с частью 1; финал не досказан.\n"
            "Сохрани различие между вариантами.\n"
            "Верни СТРОГО столько же строк, сколько фраз ниже, по одной фразе на строку, "
            "без нумерации и пояснений.\n\n"
            + rules
            + "\n\n"
            + "\n\n".join(blocks)
        )

    def raw_image_template(self) -> str:
        if self.image_prompt_template is not None:
            return self.image_prompt_template
        if self.image_prompt_file and self.image_prompt_file.is_file():
            return self.image_prompt_file.read_text(encoding="utf-8")
        return ""

    def image_prompt(
        self, story_text: str, thumb_text: str, *, variant_index: int = 1
    ) -> str:
        phrase = (thumb_text or "").strip()
        include_thumb = bool(phrase) or self.needs_text_prompt()
        template = _ensure_placeholders(
            self.raw_image_template(), include_thumb_text=include_thumb
        )
        return _safe_template_format(
            template,
            thumb_text=phrase,
            story_text=story_text.strip(),
            variant_hint=_VARIANT_HINTS.get(variant_index, _VARIANT_HINTS[1]),
        )


_BUILTIN_ID = "drama_left_text_v1"
_BUILTIN_PROMPT_FILE = _PROMPTS / "drama_left_text_v1_image.txt"
_BUILTIN_TEXT_FILE = _PROMPTS / "drama_left_text_v1_text.txt"
_BUILTIN_EXAMPLE = _ASSETS / "thumbnail_style_1.png"
_BUILTIN_META = _PROMPTS / "drama_left_text_v1_meta.json"
_BUILTIN_DEFAULT_NAME = "Драма: текст слева, конфликт справа (16:9)"
_BUILTIN_DEFAULT_DESC = (
    "ТВ-мелодрама: крупная типографика слева (жёлтый/красный/белый), "
    "герой с козырем справа."
)


def _load_builtin() -> ThumbnailPreset:
    name = _BUILTIN_DEFAULT_NAME
    description = _BUILTIN_DEFAULT_DESC
    if _BUILTIN_META.is_file():
        try:
            meta = json.loads(_BUILTIN_META.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                name = str(meta.get("name") or name).strip() or name
                description = (
                    str(meta.get("description") or description).strip() or description
                )
        except (OSError, json.JSONDecodeError):
            pass
    return ThumbnailPreset(
        id=_BUILTIN_ID,
        name=name,
        description=description,
        example_image=_BUILTIN_EXAMPLE,
        image_prompt_file=_BUILTIN_PROMPT_FILE,
        text_prompt_file=_BUILTIN_TEXT_FILE,
        builtin=True,
    )


def _slugify(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", name.strip().lower(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:48] or uuid.uuid4().hex[:8]


def _load_user_index() -> list[dict]:
    if not _USER_INDEX.is_file():
        return []
    try:
        data = json.loads(_USER_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_user_index(items: list[dict]) -> None:
    _USER_DIR.mkdir(parents=True, exist_ok=True)
    _USER_INDEX.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _preset_from_user_row(row: dict) -> ThumbnailPreset | None:
    pid = str(row.get("id") or "").strip()
    name = str(row.get("name") or "").strip()
    if not pid or not name:
        return None
    folder = _USER_DIR / pid
    prompt_path = folder / "prompt.txt"
    text_path = folder / "text_prompt.txt"
    example = Path(str(row.get("example_image") or (folder / "example.png")))
    if not example.is_absolute():
        example = _ROOT / example
    template = None
    if prompt_path.is_file():
        template = prompt_path.read_text(encoding="utf-8")
    elif row.get("prompt"):
        template = str(row["prompt"])
    if not template or not template.strip():
        return None
    text_template = ""
    if text_path.is_file():
        text_template = text_path.read_text(encoding="utf-8")
    elif "text_prompt" in row:
        text_template = str(row.get("text_prompt") or "")
    prep_path = folder / "image_prep.txt"
    prep_template = ""
    if prep_path.is_file():
        prep_template = prep_path.read_text(encoding="utf-8")
    elif "image_prep" in row:
        prep_template = str(row.get("image_prep") or "")
    return ThumbnailPreset(
        id=pid,
        name=name,
        description=str(row.get("description") or name),
        example_image=example,
        image_prompt_template=template,
        text_prompt_template=text_template,
        image_prep_prompt_template=prep_template,
        builtin=False,
    )


def get_user_presets() -> list[ThumbnailPreset]:
    out: list[ThumbnailPreset] = []
    for row in _load_user_index():
        p = _preset_from_user_row(row)
        if p:
            out.append(p)
    return out


def get_presets() -> list[ThumbnailPreset]:
    return [_load_builtin(), *get_user_presets()]


def get_preset(preset_id: str) -> ThumbnailPreset:
    for p in get_presets():
        if p.id == preset_id:
            return p
    return _load_builtin()


def preset_display_number(preset_id: str) -> int:
    """Порядковый номер мастер-промпта 1..N (как в UI)."""
    for i, p in enumerate(get_presets(), start=1):
        if p.id == preset_id:
            return i
    return 1


def default_preset_id() -> str:
    return _BUILTIN_ID


def save_builtin_preset(
    *,
    name: str,
    prompt: str,
    text_prompt: str = "",
    image_prep: str = "",
    description: str = "",
    example_image: Path | None = None,
) -> ThumbnailPreset:
    name = name.strip()
    prompt = prompt.strip()
    if not name:
        raise ValueError("Укажи название пресета")
    if not prompt:
        raise ValueError("Укажи промпт картинки (обязательно)")

    _PROMPTS.mkdir(parents=True, exist_ok=True)
    _BUILTIN_PROMPT_FILE.write_text(prompt, encoding="utf-8")
    _BUILTIN_TEXT_FILE.write_text(text_prompt, encoding="utf-8")
    # builtin image_prep пока не используем (мастер #1 без prep)
    _ = image_prep
    _BUILTIN_META.write_text(
        json.dumps(
            {
                "name": name,
                "description": (description or name).strip(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if example_image and example_image.is_file():
        _ASSETS.mkdir(parents=True, exist_ok=True)
        if example_image.resolve() != _BUILTIN_EXAMPLE.resolve():
            suffix = example_image.suffix.lower()
            if suffix == ".png":
                shutil.copy2(example_image, _BUILTIN_EXAMPLE)
            else:
                from PIL import Image

                with Image.open(example_image) as img:
                    img.convert("RGBA").save(_BUILTIN_EXAMPLE, format="PNG")
    return _load_builtin()


def save_user_preset(
    *,
    name: str,
    prompt: str,
    text_prompt: str = "",
    image_prep: str = "",
    description: str = "",
    example_image: Path | None = None,
    preset_id: str | None = None,
) -> ThumbnailPreset:
    name = name.strip()
    prompt = prompt.strip()
    if not name:
        raise ValueError("Укажи название пресета")
    if not prompt:
        raise ValueError("Укажи промпт картинки (обязательно)")

    pid = (preset_id or "").strip()
    if pid == _BUILTIN_ID:
        return save_builtin_preset(
            name=name,
            prompt=prompt,
            text_prompt=text_prompt,
            image_prep=image_prep,
            description=description,
            example_image=example_image,
        )

    items = _load_user_index()
    if not pid:
        pid = _slugify(name)
    existing_ids = {str(x.get("id")) for x in items}
    if not preset_id and pid in existing_ids:
        pid = f"{pid}-{uuid.uuid4().hex[:6]}"

    folder = _USER_DIR / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    (folder / "text_prompt.txt").write_text(text_prompt, encoding="utf-8")
    (folder / "image_prep.txt").write_text(image_prep, encoding="utf-8")

    example_dest = folder / "example.png"
    if example_image and example_image.is_file():
        suffix = example_image.suffix.lower() or ".png"
        example_dest = folder / f"example{suffix}"
        shutil.copy2(example_image, example_dest)

    row = {
        "id": pid,
        "name": name,
        "description": (description or name).strip(),
        "example_image": str(example_dest.relative_to(_ROOT))
        if example_dest.is_file()
        else "",
    }
    items = [x for x in items if str(x.get("id")) != pid]
    items.append(row)
    _save_user_index(items)
    preset = _preset_from_user_row(row)
    assert preset is not None
    return preset


def delete_user_preset(preset_id: str) -> None:
    pid = preset_id.strip()
    if not pid or pid == _BUILTIN_ID:
        raise ValueError("Нельзя удалить встроенный пресет")
    items = [x for x in _load_user_index() if str(x.get("id")) != pid]
    _save_user_index(items)
    folder = _USER_DIR / pid
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def user_presets_dir() -> Path:
    return _USER_DIR
