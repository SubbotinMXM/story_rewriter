"""Полный флоу: рерайт/рассказ с нуля -> (опц. превью) -> TTS -> видео."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from compositor.defaults import SUBSCRIBE_PATH

from compositor.pipeline import ComposeRequest, build_and_run
from rewriter import checkpoint as cp
from rewriter.cancel import CancelToken, CancelledError
from rewriter.logutil import log
from rewriter.lumean import LumeanClient, tts_wait_timeout_sec
from rewriter.openai_client import DEFAULT_BASE_URL
from rewriter.pipeline import (
    finalize_run_dir,
    rewrite_story,
    run_output_dir,
    save_story_text,
    story_output_path,
    video_output_path,
)
from rewriter.hook_story import generate_hook_story, save_story_plan
from rewriter.profession_story import StoryMeta, generate_profession_story
from rewriter.video_pipeline import copy_audio_deliverable
from rewriter.thumbnail import (
    DEFAULT_IMAGE_API_KEY,
    DEFAULT_IMAGE_BASE_URL,
    DEFAULT_IMAGE_MODEL,
    PreviewBatchResult,
    PreviewGenerator,
    ThumbnailError,
)
from rewriter.thumbnail_presets import default_preset_id

ProgressCb = Callable[[float, str], None]
StartFrom = Literal["auto", "rewrite", "tts", "video"]
StoryMode = Literal["rewrite", "profession", "hook"]


@dataclass
class FullRunRequest:
    source_text: str
    prefix: str
    overlay_text: str
    gpt_api_key: str
    gpt_base_url: str
    gpt_model: str
    lumean_api_key: str
    template_id: str
    voice_id: str
    broll_dir: Path
    head_dir: Path | None
    subscribe: bool
    subscribe_path: Path | None = None
    outro_dir: Path | None = None
    voice_speed: float | None = None
    thumbnail_enabled: bool = False
    thumbnail_preset_id: str = field(default_factory=default_preset_id)
    thumbnail_variant_count: int = 1
    image_api_key: str = DEFAULT_IMAGE_API_KEY
    image_base_url: str = DEFAULT_IMAGE_BASE_URL
    thumbnail_image_model: str = DEFAULT_IMAGE_MODEL
    story_mode: StoryMode = "rewrite"
    profession: str = ""
    hook: str = ""


@dataclass
class FullRunResult:
    video: Path
    text_file: Path
    text_chars: int
    audio_bytes: int
    resumed_from: str
    output_dir: Path
    audio_file: Path | None = None
    preview: PreviewBatchResult | None = None
    preview_error: str = ""
    story_meta: StoryMeta | None = None
    story_mode: StoryMode = "rewrite"


def _rebase_under(path: Path, old_dir: Path, new_dir: Path) -> Path:
    try:
        if path.resolve().parent == old_dir.resolve():
            return new_dir / path.name
    except OSError:
        pass
    return path


def _input_identity(req: FullRunRequest) -> str:
    if req.story_mode == "profession":
        return f"profession\n{(req.profession or '').strip()}"
    if req.story_mode == "hook":
        return f"hook\n{(req.hook or '').strip()}"
    return req.source_text


def _save_story_meta(meta: StoryMeta, out_dir: Path) -> Path | None:
    if meta.is_empty():
        return None
    path = out_dir / "story_meta.json"
    path.write_text(
        json.dumps(
            {
                "titles": meta.titles,
                "yt_titles": meta.yt_titles,
                "description": meta.description,
                "preview_phrases": meta.preview_phrases,
                "summary": meta.summary,
                "plot_turns": meta.plot_turns,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def run_full_pipeline(
    req: FullRunRequest,
    *,
    on_progress: ProgressCb | None = None,
    on_preview_phrases: Callable[[list[str]], None] | None = None,
    on_story_meta: Callable[[StoryMeta], None] | None = None,
    start_from: StartFrom = "auto",
    cancel: CancelToken | None = None,
) -> FullRunResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel:
            cancel.check()

    if req.story_mode == "profession":
        mode: StoryMode = "profession"
    elif req.story_mode == "hook":
        mode = "hook"
    else:
        mode = "rewrite"
    source_hash = cp.content_hash(_input_identity(req))
    prefix_hash = cp.content_hash(req.prefix)
    existing = cp.load_checkpoint()

    if start_from == "auto" and existing and existing.can_resume:
        if (
            existing.source_hash == source_hash
            and existing.prefix_hash == prefix_hash
        ):
            start_from = "tts" if existing.stage == "rewrite_done" else "video"
            progress(0.02, f"Продолжаю с этапа: {start_from}")
        else:
            log("Чекпоинт от другого текста - начинаю с рерайта")
            start_from = "rewrite"

    if start_from == "auto":
        start_from = "rewrite"

    resumed_from = start_from
    text = ""
    text_chars = 0
    audio_bytes = 0
    preview: PreviewBatchResult | None = None
    preview_error = ""
    story_meta: StoryMeta | None = None
    story_plan: str = ""
    story_canon: str = ""
    out_dir: Path | None = None
    text_file: Path | None = None
    audio_file: Path | None = None

    try:
        check()
        # --- 1) Текст (рерайт или рассказ с нуля) ---
        if start_from == "rewrite":
            def rewrite_prog(pct: float, msg: str) -> None:
                check()
                progress(pct * 0.40, msg)

            if mode == "profession":
                prof = (req.profession or "").strip()
                if not prof:
                    raise ValueError("Укажи профессию для рассказа с нуля")
                if not (req.gpt_api_key or "").strip():
                    raise ValueError("Нужен GPT API key")
                generated = generate_profession_story(
                    profession=prof,
                    api_key=req.gpt_api_key,
                    model=req.gpt_model,
                    base_url=req.gpt_base_url or DEFAULT_BASE_URL,
                    prefix=req.prefix,
                    on_progress=rewrite_prog,
                    cancel=cancel,
                )
                check()
                text = generated.text
                story_meta = generated.meta
                if on_story_meta and story_meta is not None:
                    on_story_meta(story_meta)
            elif mode == "hook":
                hook = (req.hook or "").strip()
                if not hook:
                    raise ValueError("Укажи хук для рассказа с нуля")
                if not (req.gpt_api_key or "").strip():
                    raise ValueError("Нужен GPT API key")
                generated_hook = generate_hook_story(
                    hook=hook,
                    api_key=req.gpt_api_key,
                    model=req.gpt_model,
                    base_url=req.gpt_base_url or DEFAULT_BASE_URL,
                    prefix=req.prefix,
                    on_progress=rewrite_prog,
                    cancel=cancel,
                )
                check()
                text = generated_hook.text
                story_meta = generated_hook.meta
                story_plan = generated_hook.plan
                story_canon = generated_hook.canon or ""
                if on_story_meta and story_meta is not None:
                    on_story_meta(story_meta)
            else:
                if not (req.source_text or "").strip():
                    raise ValueError("Вставь рассказ для рерайта")
                rewritten = rewrite_story(
                    source_text=req.source_text,
                    prefix=req.prefix,
                    api_key=req.gpt_api_key,
                    model=req.gpt_model,
                    base_url=req.gpt_base_url or DEFAULT_BASE_URL,
                    on_progress=rewrite_prog,
                    cancel=cancel,
                )
                check()
                text = rewritten.text

            text_chars = len(text)
            out_dir = run_output_dir(req.overlay_text)
            progress(0.41, f"Папка результатов: {out_dir.name}")
            text_file = save_story_text(text, story_output_path(out_dir=out_dir))
            progress(0.42, f"Текст сохранен: {out_dir.name}/{text_file.name}")
            if story_plan.strip():
                plan_path = save_story_plan(story_plan, out_dir / "story_plan.md")
                progress(0.423, f"План сохранён: {plan_path.name}")
            if story_canon.strip():
                canon_path = save_story_plan(story_canon, out_dir / "story_canon.md")
                progress(0.424, f"Канон сохранён: {canon_path.name}")
            if story_meta is not None:
                meta_path = _save_story_meta(story_meta, out_dir)
                if meta_path:
                    progress(0.425, f"Мета сохранена: {meta_path.name}")
            cp.mark_rewrite_done(
                text=text,
                source_hash=source_hash,
                prefix_hash=prefix_hash,
            )
        else:
            text = cp.read_checkpoint_text()
            text_chars = len(text)
            out_dir = run_output_dir(req.overlay_text)
            progress(0.41, f"Папка результатов: {out_dir.name}")
            text_file = save_story_text(text, story_output_path(out_dir=out_dir))
            progress(0.42, f"Беру текст из чекпоинта ({text_chars} символов)")

        assert out_dir is not None and text_file is not None

        check()
        # --- 1.5) Превью (опционально, не блокирует) ---
        if req.thumbnail_enabled and start_from == "rewrite":
            n = max(1, min(int(req.thumbnail_variant_count), 3))
            progress(
                0.44,
                f"Превью: {n} вариант(ов) → {out_dir.name}…",
            )
            gen = None
            try:
                gen = PreviewGenerator(
                    text_api_key=req.gpt_api_key,
                    text_base_url=req.gpt_base_url or DEFAULT_BASE_URL,
                    text_model=req.gpt_model,
                    image_api_key=req.image_api_key or DEFAULT_IMAGE_API_KEY,
                    image_base_url=req.image_base_url or DEFAULT_IMAGE_BASE_URL,
                    image_model=req.thumbnail_image_model or DEFAULT_IMAGE_MODEL,
                    cancel=cancel,
                    on_progress=lambda m: progress(0.46, m),
                    on_phrases=on_preview_phrases,
                )
                preview = gen.generate_batch(
                    story_text=text,
                    preset_id=req.thumbnail_preset_id or default_preset_id(),
                    variant_count=n,
                    out_dir=out_dir,
                )
                ok = len(preview.ok_paths)
                progress(0.50, f"Превью: готово {ok}/{n}")
            except CancelledError:
                raise
            except ThumbnailError as exc:
                preview_error = str(exc)
                progress(0.50, f"Превью пропущено: {preview_error}")
                log(f"Превью не создано, продолжаю флоу: {preview_error}")
            except Exception as exc:
                preview_error = f"{type(exc).__name__}: {exc}"
                progress(0.50, f"Превью пропущено: {preview_error}")
                log(f"Превью не создано, продолжаю флоу: {preview_error}")
            finally:
                if gen is not None:
                    gen.close()

        check()
        # --- 2) TTS ---
        if start_from in {"rewrite", "tts"}:
            progress(0.52, "Озвучка: создаю заказ Lumean…")
            cp.ensure_run_dir()
            audio_path = cp.AUDIO_PATH
            client = LumeanClient(req.lumean_api_key)
            if cancel:
                cancel.register(client)
            try:
                template_id = client.resolve_template_id(req.template_id)
                if template_id != req.template_id:
                    progress(0.53, f"Шаблон обновлен -> {template_id[:8]}…")

                check()
                order_id = client.create_tts_order(
                    template_id=template_id,
                    input_text=text,
                    voice_id=req.voice_id,
                    speed=req.voice_speed,
                )
                progress(0.56, f"Озвучка: заказ {order_id[:8]}…")

                def on_status(st: str) -> None:
                    progress(0.62, f"Озвучка: {st}")

                order = client.wait_order(
                    order_id,
                    timeout_sec=tts_wait_timeout_sec(len(text)),
                    on_status=on_status,
                    cancel=cancel,
                )
                check()
                progress(0.69, "Озвучка: скачиваю аудио…")
                client.download_order_audio(order, audio_path)
            finally:
                client.close()

            audio_bytes = audio_path.stat().st_size
            cp.mark_tts_done(audio_bytes=audio_bytes)
            progress(0.72, f"Аудио готово ({audio_bytes} bytes)")
            audio_file = copy_audio_deliverable(audio_path, out_dir)
        else:
            audio_path = cp.AUDIO_PATH
            audio_bytes = audio_path.stat().st_size
            progress(0.72, f"Беру аудио из чекпоинта ({audio_bytes} bytes)")
            audio_file = copy_audio_deliverable(audio_path, out_dir)

        check()
        # --- 3) Видео ---
        out = video_output_path(out_dir=out_dir)
        progress(0.75, "Сборка видео…")

        def video_prog(pct: float, msg: str) -> None:
            check()
            progress(0.75 + pct * 0.25, msg)

        build_and_run(
            ComposeRequest(
                audio=audio_path,
                broll_dir=req.broll_dir,
                head_dir=req.head_dir,
                text=req.overlay_text,
                output=out,
                subscribe=req.subscribe,
                subscribe_path=req.subscribe_path,
                outro_dir=req.outro_dir,
            ),
            on_progress=video_prog,
        )

        check()
        finished_at = datetime.now()
        old_dir = out_dir
        out_dir = finalize_run_dir(out_dir, req.overlay_text, finished_at)
        if out_dir != old_dir:
            out = _rebase_under(out, old_dir, out_dir)
            text_file = _rebase_under(text_file, old_dir, out_dir)
            if audio_file is not None:
                audio_file = _rebase_under(audio_file, old_dir, out_dir)
            if preview is not None:
                for v in preview.variants:
                    if v.path is not None:
                        v.path = _rebase_under(v.path, old_dir, out_dir)

        cp.mark_completed()
        progress(1.0, f"Готово: {out_dir.name}/ ({out.name})")
        return FullRunResult(
            video=out,
            text_file=text_file,
            text_chars=text_chars,
            audio_bytes=audio_bytes,
            resumed_from=resumed_from,
            output_dir=out_dir,
            audio_file=audio_file,
            preview=preview,
            preview_error=preview_error,
            story_meta=story_meta,
            story_mode=mode,
        )
    except CancelledError:
        log("Пайплайн остановлен пользователем")
        raise
    except Exception as exc:
        cp.mark_failed(str(exc))
        raise
