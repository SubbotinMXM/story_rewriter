"""Ролик из готового текста или готового аудио (без рерайта)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from compositor.pipeline import ComposeRequest, build_and_run
from rewriter.cancel import CancelToken, CancelledError
from rewriter.logutil import log
from rewriter.lumean import (
    DEFAULT_TEMPLATE_ID,
    DEFAULT_VOICE_ID,
    LumeanClient,
    tts_wait_timeout_sec,
)
from rewriter.openai_client import DEFAULT_BASE_URL
from rewriter.pipeline import (
    audio_output_path,
    finalize_run_dir,
    run_output_dir,
    save_story_text,
    story_output_path,
    video_output_path,
)
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

@dataclass
class ComposeParams:
    overlay_text: str
    broll_dir: Path
    head_dir: Path | None
    subscribe: bool
    subscribe_path: Path | None = None
    outro_dir: Path | None = None


@dataclass
class ThumbnailParams:
    enabled: bool = False
    preset_id: str = field(default_factory=default_preset_id)
    variant_count: int = 1
    gpt_api_key: str = ""
    gpt_base_url: str = DEFAULT_BASE_URL
    gpt_model: str = ""
    image_api_key: str = DEFAULT_IMAGE_API_KEY
    image_base_url: str = DEFAULT_IMAGE_BASE_URL
    image_model: str = DEFAULT_IMAGE_MODEL


@dataclass
class VideoRunResult:
    video: Path
    output_dir: Path
    text_file: Path | None = None
    audio_file: Path | None = None
    text_chars: int = 0
    audio_bytes: int = 0
    preview: PreviewBatchResult | None = None
    preview_error: str = ""


def _rebase_under(path: Path, old_dir: Path, new_dir: Path) -> Path:
    try:
        if path.resolve().parent == old_dir.resolve():
            return new_dir / path.name
    except OSError:
        pass
    return path


def copy_audio_deliverable(src: Path, out_dir: Path) -> Path:
    """Копия озвучки в папку результата (рядом с рассказом/роликом)."""
    dest = audio_output_path(out_dir=out_dir)
    shutil.copy2(src, dest)
    log(f"Озвучка сохранена: {out_dir.name}/{dest.name} ({dest.stat().st_size} bytes)")
    return dest


def run_preview_optional(
    *,
    story_text: str,
    out_dir: Path,
    thumb: ThumbnailParams,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
    on_phrases: Callable[[list[str]], None] | None,
    progress_at: float = 0.2,
) -> tuple[PreviewBatchResult | None, str]:
    if not thumb.enabled:
        return None, ""
    from rewriter.thumbnail_presets import get_preset

    preset = get_preset(thumb.preset_id or default_preset_id())
    if not story_text.strip() and preset.needs_story_input():
        return None, "Нет текста для превью"
    n = max(1, min(int(thumb.variant_count), 3))
    if on_progress:
        on_progress(progress_at, f"Превью: {n} вариант(ов) → {out_dir.name}…")
    gen = None
    try:
        gen = PreviewGenerator(
            text_api_key=thumb.gpt_api_key,
            text_base_url=thumb.gpt_base_url or DEFAULT_BASE_URL,
            text_model=thumb.gpt_model,
            image_api_key=thumb.image_api_key or DEFAULT_IMAGE_API_KEY,
            image_base_url=thumb.image_base_url or DEFAULT_IMAGE_BASE_URL,
            image_model=thumb.image_model or DEFAULT_IMAGE_MODEL,
            cancel=cancel,
            on_progress=lambda m: on_progress(progress_at + 0.02, m)
            if on_progress
            else None,
            on_phrases=on_phrases,
        )
        preview = gen.generate_batch(
            story_text=story_text,
            preset_id=thumb.preset_id or default_preset_id(),
            variant_count=n,
            out_dir=out_dir,
        )
        if on_progress:
            on_progress(progress_at + 0.08, f"Превью: готово {len(preview.ok_paths)}/{n}")
        return preview, ""
    except CancelledError:
        raise
    except ThumbnailError as exc:
        err = str(exc)
        if on_progress:
            on_progress(progress_at + 0.08, f"Превью пропущено: {err}")
        log(f"Превью не создано, продолжаю: {err}")
        return None, err
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        if on_progress:
            on_progress(progress_at + 0.08, f"Превью пропущено: {err}")
        log(f"Превью не создано, продолжаю: {err}")
        return None, err
    finally:
        if gen is not None:
            gen.close()


def synthesize_tts(
    *,
    text: str,
    dest: Path,
    lumean_api_key: str,
    template_id: str,
    voice_id: str,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
    voice_speed: float | None = None,
) -> int:
    if on_progress:
        on_progress(0.35, "Озвучка: создаю заказ Lumean…")
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = LumeanClient(lumean_api_key)
    if cancel:
        cancel.register(client)
    try:
        tid = client.resolve_template_id(template_id or DEFAULT_TEMPLATE_ID)
        if cancel:
            cancel.check()
        order_id = client.create_tts_order(
            template_id=tid,
            input_text=text,
            voice_id=voice_id or DEFAULT_VOICE_ID,
            speed=voice_speed,
        )
        if on_progress:
            on_progress(0.40, f"Озвучка: заказ {order_id[:8]}…")

        def on_status(st: str) -> None:
            if on_progress:
                on_progress(0.50, f"Озвучка: {st}")

        order = client.wait_order(
            order_id,
            timeout_sec=tts_wait_timeout_sec(len(text)),
            on_status=on_status,
            cancel=cancel,
        )
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(0.60, "Озвучка: скачиваю аудио…")
        client.download_order_audio(order, dest)
    finally:
        client.close()
    return dest.stat().st_size


def compose_video(
    *,
    audio: Path,
    compose: ComposeParams,
    out_dir: Path,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
) -> Path:
    out = video_output_path(out_dir=out_dir)
    if on_progress:
        on_progress(0.75, "Сборка видео…")

    def video_prog(pct: float, msg: str) -> None:
        if cancel:
            cancel.check()
        if on_progress:
            on_progress(0.75 + pct * 0.24, msg)

    build_and_run(
        ComposeRequest(
            audio=audio,
            broll_dir=compose.broll_dir,
            head_dir=compose.head_dir,
            text=compose.overlay_text,
            output=out,
            subscribe=compose.subscribe,
            subscribe_path=compose.subscribe_path,
            outro_dir=compose.outro_dir,
        ),
        on_progress=video_prog,
    )
    return out


def finalize_outputs(
    *,
    out_dir: Path,
    overlay_text: str,
    video: Path,
    text_file: Path | None,
    audio_file: Path | None,
    preview: PreviewBatchResult | None,
) -> tuple[Path, Path, Path | None, Path | None]:
    finished_at = datetime.now()
    old_dir = out_dir
    out_dir = finalize_run_dir(out_dir, overlay_text, finished_at)
    if out_dir != old_dir:
        video = _rebase_under(video, old_dir, out_dir)
        if text_file is not None:
            text_file = _rebase_under(text_file, old_dir, out_dir)
        if audio_file is not None:
            audio_file = _rebase_under(audio_file, old_dir, out_dir)
        if preview is not None:
            for v in preview.variants:
                if v.path is not None:
                    v.path = _rebase_under(v.path, old_dir, out_dir)
    return out_dir, video, text_file, audio_file


def run_video_from_text(
    *,
    story_text: str,
    compose: ComposeParams,
    lumean_api_key: str,
    template_id: str,
    voice_id: str,
    thumb: ThumbnailParams | None = None,
    work_audio: Path,
    on_progress: ProgressCb | None = None,
    on_preview_phrases: Callable[[list[str]], None] | None = None,
    cancel: CancelToken | None = None,
    voice_speed: float | None = None,
) -> VideoRunResult:
    """Готовый .txt → (превью) → TTS → видео. Без рерайта и без чекпоинта."""

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel:
            cancel.check()

    text = (story_text or "").strip()
    if not text:
        raise ValueError("Пустой текст рассказа")
    thumb = thumb or ThumbnailParams()

    check()
    out_dir = run_output_dir(compose.overlay_text)
    progress(0.05, f"Папка результатов: {out_dir.name}")
    text_file = save_story_text(text, story_output_path(out_dir=out_dir))
    progress(0.08, f"Текст сохранен: {out_dir.name}/{text_file.name}")

    check()
    preview, preview_error = run_preview_optional(
        story_text=text,
        out_dir=out_dir,
        thumb=thumb,
        cancel=cancel,
        on_progress=progress,
        on_phrases=on_preview_phrases,
        progress_at=0.12,
    )

    check()
    audio_bytes = synthesize_tts(
        text=text,
        dest=work_audio,
        lumean_api_key=lumean_api_key,
        template_id=template_id,
        voice_id=voice_id,
        voice_speed=voice_speed,
        cancel=cancel,
        on_progress=progress,
    )
    progress(0.65, f"Аудио готово ({audio_bytes} bytes)")
    audio_file = copy_audio_deliverable(work_audio, out_dir)

    check()
    video = compose_video(
        audio=work_audio,
        compose=compose,
        out_dir=out_dir,
        cancel=cancel,
        on_progress=progress,
    )

    check()
    out_dir, video, text_file, audio_file = finalize_outputs(
        out_dir=out_dir,
        overlay_text=compose.overlay_text,
        video=video,
        text_file=text_file,
        audio_file=audio_file,
        preview=preview,
    )
    progress(1.0, f"Готово: {out_dir.name}/ ({video.name})")
    return VideoRunResult(
        video=video,
        output_dir=out_dir,
        text_file=text_file,
        audio_file=audio_file,
        text_chars=len(text),
        audio_bytes=audio_bytes,
        preview=preview,
        preview_error=preview_error,
    )


def run_video_from_audio(
    *,
    audio_path: Path,
    compose: ComposeParams,
    thumb: ThumbnailParams | None = None,
    preview_story_text: str = "",
    on_progress: ProgressCb | None = None,
    on_preview_phrases: Callable[[list[str]], None] | None = None,
    cancel: CancelToken | None = None,
) -> VideoRunResult:
    """Готовое аудио → (опц. превью по тексту) → видео."""

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel:
            cancel.check()

    src = Path(audio_path)
    if not src.is_file():
        raise FileNotFoundError(f"Нет аудиофайла: {src}")
    thumb = thumb or ThumbnailParams()

    check()
    out_dir = run_output_dir(compose.overlay_text)
    progress(0.05, f"Папка результатов: {out_dir.name}")

    audio_file = copy_audio_deliverable(src, out_dir)
    audio_bytes = audio_file.stat().st_size
    progress(0.12, f"Аудио: {audio_file.name} ({audio_bytes} bytes)")

    text_file = None
    story = (preview_story_text or "").strip()
    if story:
        text_file = save_story_text(story, story_output_path(out_dir=out_dir))
        progress(0.15, f"Текст для превью: {text_file.name}")

    check()
    preview, preview_error = run_preview_optional(
        story_text=story,
        out_dir=out_dir,
        thumb=thumb,
        cancel=cancel,
        on_progress=progress,
        on_phrases=on_preview_phrases,
        progress_at=0.20,
    )

    check()
    video = compose_video(
        audio=src,
        compose=compose,
        out_dir=out_dir,
        cancel=cancel,
        on_progress=progress,
    )

    check()
    out_dir, video, text_file, audio_file = finalize_outputs(
        out_dir=out_dir,
        overlay_text=compose.overlay_text,
        video=video,
        text_file=text_file,
        audio_file=audio_file,
        preview=preview,
    )
    progress(1.0, f"Готово: {out_dir.name}/ ({video.name})")
    return VideoRunResult(
        video=video,
        output_dir=out_dir,
        text_file=text_file,
        audio_file=audio_file,
        text_chars=len(story),
        audio_bytes=audio_bytes,
        preview=preview,
        preview_error=preview_error,
    )
