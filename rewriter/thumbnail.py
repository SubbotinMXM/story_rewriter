"""Генерация превью: GPT-текст (pipeline API) + 3 параллельных image-запроса без ретраев."""

from __future__ import annotations

import base64
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from openai import OpenAI
from PIL import Image

from rewriter.cancel import CancelToken, CancelledError
from rewriter.logutil import log
from rewriter.openai_client import DEFAULT_BASE_URL, _SAFE_HEADERS
from rewriter.portrait_chrome import render_portrait_chrome
from rewriter.preset_titles import has_title_catalog, next_title
from rewriter.thumb_text import (
    clip_to_word_limit,
    force_usable_thumb_phrase,
    normalize_thumb_phrase,
    salvage_thumb_phrase,
    validate_thumb_phrase,
    MAX_THUMB_WORDS,
)
from rewriter.thumbnail_presets import get_preset

DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_BASE_URL = "https://closeai.com.ru/v1"
DEFAULT_IMAGE_API_KEY = (
    "sk-33a9de71f72984bb0daeb00234c0e443097c7b71dad5d6f48363650dc3fbc17f"
)
DEFAULT_VARIANT_COUNT = 3
MAX_STORY_CTX = 1800  # в image-промпт (визуальный контекст, не хук)

ProgressCb = Callable[[str], None]
PhrasesCb = Callable[[list[str]], None]


class ThumbnailError(RuntimeError):
    pass


@dataclass
class VariantResult:
    index: int
    text: str = ""
    path: Path | None = None
    error: str = ""


@dataclass
class PreviewBatchResult:
    text: str  # все фразы одной простынёй для UI
    variants: list[VariantResult] = field(default_factory=list)

    @property
    def ok_paths(self) -> list[Path]:
        return [v.path for v in self.variants if v.path is not None]

    @property
    def errors(self) -> list[str]:
        return [f"#{v.index}: {v.error}" for v in self.variants if v.error]

    @property
    def phrases(self) -> list[str]:
        return [v.text for v in self.variants if v.text]


def desktop_dir() -> Path:
    return Path.home() / "Desktop"


def thumbnail_variant_path(
    index: int,
    now: datetime | None = None,
    *,
    out_dir: Path | None = None,
) -> Path:
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = out_dir or desktop_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"обложка-{stamp}-{index}.png"


def ping_image_api(
    *,
    api_key: str,
    base_url: str = DEFAULT_IMAGE_BASE_URL,
    image_model: str = DEFAULT_IMAGE_MODEL,
) -> str:
    """Лёгкая проверка ключа/URL без генерации картинки (GET /models)."""
    url = (base_url or DEFAULT_IMAGE_BASE_URL).strip().rstrip("/")
    key = (api_key or "").strip()
    if not key:
        raise ThumbnailError("Пустой Image API key")
    log(f"PING [image] → GET {url}/models")
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as http:
            r = http.get(
                f"{url}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        log(f"PING [image] FAIL ({ms} ms): {type(exc).__name__}: {exc}")
        raise ThumbnailError(f"Image API недоступен: {exc}") from exc

    models: list[str] = []
    if isinstance(data, dict):
        for item in data.get("data") or []:
            mid = str((item or {}).get("id") or "").strip()
            if mid:
                models.append(mid)
    ms = int((time.monotonic() - t0) * 1000)
    want = (image_model or DEFAULT_IMAGE_MODEL).strip()
    has = want in models if models else False
    summary = (
        f"OK ({ms} ms). Моделей: {len(models)}. "
        f"{'Есть' if has else 'Нет'} «{want}»."
    )
    if models:
        summary += " Список: " + ", ".join(models[:12])
        if len(models) > 12:
            summary += "…"
    log(f"PING [image] {summary}")
    return summary


def _make_client(*, api_key: str, base_url: str, max_retries: int) -> OpenAI:
    url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    http = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), trust_env=False)
    return OpenAI(
        api_key=api_key.strip(),
        base_url=url,
        http_client=http,
        default_headers=_SAFE_HEADERS,
        max_retries=max_retries,
    )


def _parse_image_prep_response(raw: str) -> tuple[str, str, str]:
    """Разбор ответа meta-промпта → (героиня, готовый промпт, негатив)."""
    text = (raw or "").strip()
    if not text:
        return "", "", ""

    heroine = ""
    ready = ""
    negative = ""

    m_h = re.search(
        r"СЛУЧАЙНАЯ\s+ГЕРОИНЯ\s*:?\s*(.*?)(?=ГОТОВЫЙ\s+ПРОМП?Т|$)",
        text,
        flags=re.I | re.S,
    )
    if m_h:
        heroine = re.sub(r"\s+", " ", m_h.group(1)).strip()
        if len(heroine) > 500:
            heroine = heroine[:500] + "…"

    m_r = re.search(
        r"ГОТОВЫЙ\s+ПРОМП?Т\s+(?:ДЛЯ\s+ГЕНЕРАЦИИ)?\s*:?\s*(.*?)(?=\n\s*(?:Негативный|НЕГАТИВНЫЙ|3\.\s*В\s+самом)|$)",
        text,
        flags=re.I | re.S,
    )
    if m_r:
        ready = m_r.group(1).strip()
        ready = re.sub(r"^```(?:text)?\s*", "", ready)
        ready = re.sub(r"\s*```$", "", ready).strip()

    m_n = re.search(
        r"(?:Негативный\s+промп?т|НЕГАТИВНЫЙ\s+ПРОМП?Т)\s*:?\s*(.*)\s*$",
        text,
        flags=re.I | re.S,
    )
    if m_n:
        negative = re.sub(r"\s+", " ", m_n.group(1)).strip()
        if len(negative) > 800:
            negative = negative[:800] + "…"

    return heroine, ready, negative


class PreviewGenerator:
    def __init__(
        self,
        *,
        text_api_key: str,
        text_base_url: str,
        text_model: str,
        image_api_key: str = DEFAULT_IMAGE_API_KEY,
        image_base_url: str = DEFAULT_IMAGE_BASE_URL,
        image_model: str = DEFAULT_IMAGE_MODEL,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
        on_phrases: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.text_base_url = (text_base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.image_base_url = (image_base_url or DEFAULT_IMAGE_BASE_URL).strip().rstrip(
            "/"
        )
        self.text_model = text_model.strip()
        self.image_model = (image_model or DEFAULT_IMAGE_MODEL).strip()
        self.cancel = cancel
        self.on_progress = on_progress
        self.on_phrases = on_phrases
        self.text_client = _make_client(
            api_key=text_api_key, base_url=self.text_base_url, max_retries=1
        )
        self.image_client = _make_client(
            api_key=image_api_key or DEFAULT_IMAGE_API_KEY,
            base_url=self.image_base_url,
            max_retries=0,
        )
        if cancel:
            cancel.register(self)

    def close(self) -> None:
        for c in (self.text_client, self.image_client):
            try:
                c.close()
            except Exception:
                pass

    def _step(self, msg: str) -> None:
        # on_progress в пайплайне сам пишет в log — не дублируем
        if self.on_progress:
            try:
                self.on_progress(msg)
            except Exception:
                pass
        else:
            log(msg)

    def _check(self) -> None:
        if self.cancel:
            self.cancel.check()

    def generate_batch(
        self,
        *,
        story_text: str,
        preset_id: str,
        variant_count: int = DEFAULT_VARIANT_COUNT,
        out_dir: Path | None = None,
    ) -> PreviewBatchResult:
        self._check()
        preset = get_preset(preset_id)
        n = max(1, min(int(variant_count), 3))
        self._step(
            f"[превью] старт: preset={preset.id} story_chars={len(story_text)} "
            f"text_model={self.text_model} image_model={self.image_model} variants={n}"
        )
        self._step(
            f"[превью] text API={self.text_base_url} | image API={self.image_base_url}"
        )

        use_title_catalog = has_title_catalog(preset.id)
        if use_title_catalog:
            # Каталог заголовков (напр. woman_random_portrait_v1): без GPT-фраз.
            phrases = [next_title(preset.id) for _ in range(n)]
            self._step(
                f"[превью] мастер {preset.id}: каталог заголовков "
                f"({n} шт., PIL-хром после image)"
            )
        elif preset.needs_text_prompt():
            phrases = self._generate_phrases(story_text, preset, count=n)
            phrases = self._validate_or_repair_many(phrases, preset)
            n = len(phrases)
        else:
            self._step(
                f"[превью] мастер {preset.id}: текстовый промпт пуст — "
                f"картинки без надписи ({n} шт.)"
            )
            phrases = [""] * n
        if self.on_phrases:
            try:
                self.on_phrases(list(phrases))
            except Exception:
                pass
        for i, ph in enumerate(phrases, start=1):
            label = ph if ph else "(без текста на превью)"
            self._step(f"[превью] фраза #{i}: {label!r}")

        stamp = datetime.now()
        paths = [
            thumbnail_variant_path(i, stamp, out_dir=out_dir) for i in range(1, n + 1)
        ]
        story_ctx = story_text.strip()
        if len(story_ctx) > MAX_STORY_CTX:
            self._step(
                f"[превью] контекст для картинки обрезан {len(story_ctx)}→{MAX_STORY_CTX}"
            )
            story_ctx = story_ctx[:MAX_STORY_CTX] + "…"

        use_prep = preset.needs_image_prep()
        if use_prep:
            self._step(
                f"[превью] image_prep: GPT соберёт {n} случайных image-промпт(ов), "
                "затем генерация картинок…"
            )
        else:
            self._step(
                f"[превью] параллельно {n} image-запроса "
                "(по 1 на поток, без ретраев)…"
            )

        def one(idx: int, out: Path, phrase: str) -> VariantResult:
            if self.cancel and self.cancel.is_cancelled():
                return VariantResult(index=idx, text=phrase, error="остановлено")
            try:
                label = phrase
                if use_prep:
                    heroine, prompt = self._prepare_image_prompt(
                        preset, variant_index=idx
                    )
                    if heroine:
                        label = heroine if not phrase else f"{phrase}\n---\n{heroine}"
                    self._step(
                        f"[превью #{idx}] image_prep OK, prompt_chars={len(prompt)}"
                    )
                else:
                    prompt = preset.image_prompt(
                        story_ctx, phrase, variant_index=idx
                    )
                self._step(
                    f"[превью #{idx}] REQ images.generate model={self.image_model} "
                    f"prompt_chars={len(prompt)}"
                )
                t0 = time.monotonic()
                img = self.image_client.images.generate(
                    model=self.image_model,
                    prompt=prompt,
                    size="1280x720",
                )
                data = None
                if getattr(img, "data", None):
                    data = getattr(img.data[0], "b64_json", None)
                if not data:
                    raise ThumbnailError("API не вернул изображение")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(base64.b64decode(data))
                _normalize_16_9(out)
                if use_title_catalog and phrase.strip():
                    render_portrait_chrome(out, phrase)
                    self._step(f"[превью #{idx}] PIL-хром: {phrase!r}")
                ms = int((time.monotonic() - t0) * 1000)
                self._step(f"[превью #{idx}] OK ({ms} ms): {out.name}")
                return VariantResult(index=idx, text=label, path=out)
            except CancelledError:
                return VariantResult(index=idx, text=phrase, error="остановлено")
            except Exception as exc:
                if self.cancel and self.cancel.is_cancelled():
                    return VariantResult(index=idx, text=phrase, error="остановлено")
                msg = f"{type(exc).__name__}: {exc}"
                self._step(f"[превью #{idx}] ERR (без ретрая): {msg}")
                return VariantResult(index=idx, text=phrase, error=msg)

        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = {
                pool.submit(one, i + 1, paths[i], phrases[i]): i + 1 for i in range(n)
            }
            by_idx: dict[int, VariantResult] = {}
            try:
                for fut in as_completed(futs):
                    if self.cancel and self.cancel.is_cancelled():
                        break
                    res = fut.result()
                    by_idx[res.index] = res
            except CancelledError:
                pass
            for i in range(1, n + 1):
                if i not in by_idx:
                    by_idx[i] = VariantResult(
                        index=i,
                        text=phrases[i - 1] if i - 1 < len(phrases) else "",
                        error="остановлено"
                        if (self.cancel and self.cancel.is_cancelled())
                        else "нет результата",
                    )
            variants = [by_idx[i] for i in range(1, n + 1)]

        if self.cancel and self.cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")

        ok = sum(1 for v in variants if v.path)
        summary = "\n".join(f"#{v.index}: {v.text}" for v in variants)
        self._step(f"[превью] готово: картинок {ok}/{n}")
        return PreviewBatchResult(text=summary, variants=variants)

    def _prepare_image_prompt(
        self, preset, *, variant_index: int
    ) -> tuple[str, str]:
        """GPT: случайная внешность → (краткий лог, готовый image-промпт)."""
        self._check()
        meta = preset.raw_image_prep_template().strip()
        if not meta:
            raise ThumbnailError("Пустой image_prep у мастер-промпта")
        seed = (
            f"\n\nЗАПУСК #{variant_index}: сделай НОВУЮ случайную комбинацию "
            f"(seed={variant_index}-{int(time.time() * 1000) % 10_000_000}). "
            "Не копируй предыдущие варианты из этого чата."
        )
        self._step(
            f"[превью #{variant_index}] image_prep GPT… "
            f"model={self.text_model} prompt_chars={len(meta) + len(seed)}"
        )
        t0 = time.monotonic()
        try:
            resp = self.text_client.chat.completions.create(
                model=self.text_model,
                messages=[{"role": "user", "content": meta + seed}],
            )
        except Exception as exc:
            if self.cancel and self.cancel.is_cancelled():
                raise CancelledError("Остановлено пользователем") from exc
            raise ThumbnailError(
                f"image_prep GPT: {type(exc).__name__}: {exc}"
            ) from exc
        raw = ""
        if resp.choices:
            raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            raise ThumbnailError("image_prep: пустой ответ GPT")
        ms = int((time.monotonic() - t0) * 1000)
        heroine, ready, negative = _parse_image_prep_response(raw)
        if not ready:
            # fallback: весь ответ как промпт, если блоки не размечены
            ready = raw
            self._step(
                f"[превью #{variant_index}] image_prep: блок «ГОТОВЫЙ ПРОМТ» "
                f"не найден — беру весь ответ ({ms} ms)"
            )
        else:
            self._step(
                f"[превью #{variant_index}] image_prep OK ({ms} ms), "
                f"ready_chars={len(ready)}"
            )
        if negative and "без текста" not in ready.casefold():
            ready = ready.rstrip() + "\n\nНегативный промпт / avoid: " + negative
        return heroine, ready

    def _parse_phrase_lines(self, raw: str, *, count: int) -> list[str]:
        lines: list[str] = []
        for line in (raw or "").splitlines():
            s = line.strip()
            if not s:
                continue
            # убрать нумерацию вида "1.", "1)", "#1", "1 "
            s = re.sub(r"^(?:#?\d+(?:[.)]|\s)+|[-—]\s+)", "", s).strip()
            # если остался ведущий #3 без пробела-разделителя
            s = re.sub(r"^#\d+\s*", "", s).strip()
            if not s:
                continue
            s = normalize_thumb_phrase(s)
            s = clip_to_word_limit(s, max_words=MAX_THUMB_WORDS)
            if s:
                lines.append(s)
            if len(lines) >= count:
                break
        return lines

    def _generate_phrases(
        self, story_text: str, preset, *, count: int
    ) -> list[str]:
        self._check()
        story = (story_text or "").strip()
        prompt = preset.text_prompt_multi(story, count=count)
        self._step(
            f"[превью текст] GPT chat… model={self.text_model} "
            f"variants={count} story_chars={len(story)} prompt_chars={len(prompt)} "
            "(полный рассказ, без обрезки)"
        )
        t0 = time.monotonic()
        try:
            resp = self.text_client.chat.completions.create(
                model=self.text_model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            if self.cancel and self.cancel.is_cancelled():
                raise CancelledError("Остановлено пользователем") from exc
            name = type(exc).__name__
            hint = ""
            if "Timeout" in name or "timeout" in str(exc).lower():
                hint = (
                    " Провайдер не успел ответить на длинный запрос. "
                    "Попробуй ещё раз или другую GPT-модель."
                )
            elif "disconnect" in str(exc).lower() or "Connection" in name:
                hint = " Сервер оборвал соединение (часто на тяжёлом prompt)."
            raise ThumbnailError(
                f"Ошибка генерации текстов превью: {exc}.{hint}"
            ) from exc
        raw = (resp.choices[0].message.content or "").strip()
        phrases = self._parse_phrase_lines(raw, count=count)
        ms = int((time.monotonic() - t0) * 1000)
        self._step(f"[превью текст] OK ({ms} ms): получено {len(phrases)}/{count}")
        if len(phrases) < count:
            raise ThumbnailError(
                f"GPT вернул {len(phrases)} фраз(ы) вместо {count}. Сырой ответ:\n{raw}"
            )
        # дедуп: если две одинаковые — ошибка (без доп. запроса)
        lowered = [p.casefold() for p in phrases]
        if len(set(lowered)) < len(lowered):
            raise ThumbnailError(
                "GPT вернул неуникальные фразы:\n" + "\n".join(phrases)
            )
        return phrases[:count]

    def _validate_or_repair_many(self, phrases: list[str], preset) -> list[str]:
        """Всегда вернуть ровно len(phrases) фраз — столько же картинок, сколько просили."""
        out = [normalize_thumb_phrase(p) for p in phrases]
        bad: list[tuple[int, str, list[str]]] = []
        for i, ph in enumerate(out):
            errors = validate_thumb_phrase(ph)
            if errors:
                salvaged = salvage_thumb_phrase(ph)
                err2 = validate_thumb_phrase(salvaged)
                if not err2:
                    out[i] = salvaged
                    self._step(
                        f"[превью текст] #{i + 1} починено локально: {salvaged!r}"
                    )
                else:
                    bad.append((i + 1, ph, errors))
                    self._step(
                        f"[превью текст] #{i + 1} проблемы: {'; '.join(errors)}"
                    )
            else:
                self._step(f"[превью текст] #{i + 1} проверка: ок")

        if bad:
            self._check()
            repair = preset.repair_prompt_multi(bad)
            self._step(
                f"[превью текст] один repair-запрос для {len(bad)} фраз(ы)…"
            )
            try:
                resp = self.text_client.chat.completions.create(
                    model=self.text_model,
                    messages=[{"role": "user", "content": repair}],
                )
                fixed_lines = self._parse_phrase_lines(
                    resp.choices[0].message.content or "", count=len(bad)
                )
            except Exception as exc:
                if self.cancel and self.cancel.is_cancelled():
                    raise CancelledError("Остановлено пользователем") from exc
                self._step(f"[превью текст] repair упал ({exc}), salvage локально")
                fixed_lines = []

            for idx, (num, old, _) in enumerate(bad):
                candidate = fixed_lines[idx] if idx < len(fixed_lines) else old
                candidate = normalize_thumb_phrase(candidate)
                if validate_thumb_phrase(candidate):
                    candidate = salvage_thumb_phrase(candidate)
                if validate_thumb_phrase(candidate):
                    candidate = force_usable_thumb_phrase(candidate)
                    self._step(
                        f"[превью текст] #{num} force OK (кавычки могли снять): "
                        f"{candidate!r}"
                    )
                else:
                    self._step(f"[превью текст] #{num} repair/salvage OK: {candidate!r}")
                out[num - 1] = candidate

        # финал: никогда не урезаем список — ровно столько, сколько запросили
        final: list[str] = []
        for i, ph in enumerate(out):
            ph2 = ph
            if validate_thumb_phrase(ph2):
                ph2 = salvage_thumb_phrase(ph2)
            if validate_thumb_phrase(ph2):
                ph2 = force_usable_thumb_phrase(ph2)
                self._step(
                    f"[превью текст] #{i + 1} доведено до usable: {ph2!r}"
                )
            final.append(ph2)
        return final

    def _generate_phrase(self, story_text: str, preset) -> str:
        return self._generate_phrases(story_text, preset, count=1)[0]

    def _validate_or_repair(self, phrase: str, preset) -> str:
        return self._validate_or_repair_many([phrase], preset)[0]


def _normalize_16_9(path: Path) -> None:
    with Image.open(path) as im:
        w, h = im.size
        target_w, target_h = 1280, 720
        target_ratio = target_w / target_h
        ratio = w / h if h else target_ratio
        if abs(ratio - target_ratio) > 0.001:
            if ratio > target_ratio:
                new_w = int(h * target_ratio)
                x = (w - new_w) // 2
                im = im.crop((x, 0, x + new_w, h))
            else:
                new_h = int(w / target_ratio)
                y = (h - new_h) // 2
                im = im.crop((0, y, w, y + new_h))
        im = im.resize((target_w, target_h), Image.LANCZOS)
        im.save(path, format="PNG")
