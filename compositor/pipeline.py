"""Сборка FFmpeg filter_complex и запуск рендера."""

from __future__ import annotations

import random
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from compositor.defaults import (
    AUDIO_BITRATE,
    CORNERS,
    HEAD_WIDTH_RATIO,
    OUT_FPS,
    OUT_HEIGHT,
    OUT_WIDTH,
    OVERLAY_MARGIN,
    SUBSCRIBE_BLEND,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_MAX_WIDTH_RATIO,
    SUBSCRIBE_PATH,
    SUBSCRIBE_SIMILARITY,
    SUBSCRIBE_START_MAX,
    SUBSCRIBE_START_MIN,
    SUBSCRIBE_VOLUME,
    VIDEO_CRF,
    VIDEO_PRESET,
)
from compositor.picker import pick_broll, pick_effect, pick_head, pick_outro
from compositor.text_overlay import render_text_png
from compositor.utils import find_ffmpeg, has_audio_stream, probe_duration


@dataclass
class ComposeRequest:
    audio: Path
    broll_dir: Path
    head_dir: Path | None
    text: str
    output: Path
    seed: int | None = None
    subscribe: bool = False
    subscribe_path: Path | None = Path(SUBSCRIBE_PATH)
    outro_dir: Path | None = None


@dataclass
class ComposeResult:
    output: Path
    duration: float
    broll: list[Path]
    head: Path | None
    effect: str
    head_corner: str
    text_corner: str
    subscribe: bool
    outro: Path | None = None


ProgressCb = Callable[[float, str], None]


def _corner_xy(corner: str, margin: int) -> tuple[str, str]:
    mapping = {
        "tl": (f"{margin}", f"{margin}"),
        "tr": (f"W-w-{margin}", f"{margin}"),
        "bl": (f"{margin}", f"H-h-{margin}"),
        "br": (f"W-w-{margin}", f"H-h-{margin}"),
    }
    return mapping[corner]


def _pick_corners(rng) -> tuple[str, str]:
    head_corner = rng.choice(CORNERS)
    text_corner = rng.choice([c for c in CORNERS if c != head_corner])
    return head_corner, text_corner


def _scale_crop(label_in: str, label_out: str) -> str:
    w, h, fps = OUT_WIDTH, OUT_HEIGHT, OUT_FPS
    return (
        f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[{label_out}]"
    )


def build_and_run(
    req: ComposeRequest,
    on_progress: ProgressCb | None = None,
) -> ComposeResult:
    rng = random.Random(req.seed)
    ffmpeg = find_ffmpeg()
    duration = probe_duration(req.audio)

    broll = pick_broll(req.broll_dir, rng)
    head = pick_head(req.head_dir, rng)
    use_head = head is not None
    effect_name, effect_filter = pick_effect(rng)
    head_corner, text_corner = _pick_corners(rng)

    n = len(broll)
    slot = duration / n
    head_w = int(OUT_WIDTH * HEAD_WIDTH_RATIO)

    use_sub = req.subscribe
    sub_path = req.subscribe_path
    sub_start = 0.0
    sub_dur = 0.0
    if use_sub:
        if not sub_path.is_file():
            # Подписка — опциональная фича. На другом Mac ассет может отсутствовать.
            # Вместо падения просто пропустим overlay.
            use_sub = False
            print(f"[WARN] Анимация подписки не найдена, пропускаю: {sub_path}")
        else:
            sub_dur = probe_duration(sub_path)
            sub_start = rng.uniform(SUBSCRIBE_START_MIN, SUBSCRIBE_START_MAX)
            if sub_start + sub_dur > duration:
                # если ролик короче — всё равно ставим, ffmpeg обрежет через enable
                pass

    req.output.parent.mkdir(parents=True, exist_ok=True)
    text_png = req.output.with_suffix(".text.png")
    render_text_png(req.text, text_png, text_corner)

    # inputs:
    # 0=audio
    # 1..n=broll
    # n+1[+head?]=head (optional)
    # next=text png
    # next=subscribe (optional)
    cmd: list[str] = [ffmpeg, "-y", "-i", str(req.audio)]
    for clip in broll:
        cmd += ["-stream_loop", "-1", "-t", f"{slot:.3f}", "-i", str(clip)]
    if use_head:
        cmd += ["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", str(head)]
    cmd += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(text_png)]
    if use_sub:
        cmd += ["-i", str(sub_path)]

    filters: list[str] = []
    concat_inputs: list[str] = []

    for i in range(n):
        inp = f"{i + 1}:v"
        scaled = f"b{i}"
        trimmed = f"bt{i}"
        filters.append(_scale_crop(inp, scaled))
        filters.append(
            f"[{scaled}]trim=duration={slot:.3f},setpts=PTS-STARTPTS[{trimmed}]"
        )
        concat_inputs.append(f"[{trimmed}]")

    filters.append("".join(concat_inputs) + f"concat=n={n}:v=1:a=0[bg0]")
    filters.append(f"[bg0]{effect_filter}[bg]")

    text_in_idx = n + 1 + (1 if use_head else 0)
    text_in = f"{text_in_idx}:v"
    base_label = "withtext"
    if use_head:
        head_in = f"{n + 1}:v"
        filters.append(
            f"[{head_in}]scale={head_w}:-2,setsar=1,fps={OUT_FPS},format=yuv420p,"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[head]"
        )
        ox, oy = _corner_xy(head_corner, OVERLAY_MARGIN)
        filters.append(f"[bg][head]overlay=x={ox}:y={oy}:format=auto[withhead]")
    else:
        filters.append("[bg]null[withhead]")

    filters.append(
        f"[{text_in}]format=rgba,fps={OUT_FPS}[txt];"
        f"[withhead][txt]overlay=0:0:format=auto[{base_label}]"
    )

    if use_sub:
        sub_in_idx = text_in_idx + 1
        sub_in = f"{sub_in_idx}"
        sub_w = int(OUT_WIDTH * SUBSCRIBE_MAX_WIDTH_RATIO)
        end = sub_start + sub_dur
        filters.append(
            f"[{sub_in}:v]fps={OUT_FPS},"
            f"chromakey={SUBSCRIBE_CHROMA}:{SUBSCRIBE_SIMILARITY}:{SUBSCRIBE_BLEND},"
            f"format=yuva420p,"
            f"scale={sub_w}:-2:flags=lanczos,"
            f"setpts=PTS+{sub_start:.3f}/TB[subv]"
        )
        filters.append(
            f"[{base_label}][subv]overlay=(W-w)/2:(H-h)/2:format=auto:"
            f"eof_action=pass:"
            f"enable='between(t\\,{sub_start:.3f}\\,{end:.3f})'[vout]"
        )
        # аудио: основная дорожка + подписка (−20%) со сдвигом
        delay_ms = int(sub_start * 1000)
        filters.append(f"[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a0]")
        filters.append(
            f"[{sub_in}:a]volume={SUBSCRIBE_VOLUME},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms}[a1]"
        )
        filters.append(
            "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        audio_map = "[aout]"
    else:
        filters.append(f"[{base_label}]null[vout]")
        audio_map = "0:a"

    filter_complex = ";".join(filters)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        audio_map,
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-shortest",
        "-movflags",
        "+faststart",
        str(req.output),
    ]

    if on_progress:
        on_progress(0.0, "Старт рендера…")

    try:
        _run_ffmpeg(cmd, duration, on_progress)
    finally:
        if text_png.exists():
            text_png.unlink(missing_ok=True)

    outro_path = _maybe_append_outro(
        req.output,
        req.outro_dir,
        rng=rng,
        on_progress=on_progress,
    )

    return ComposeResult(
        output=req.output,
        duration=duration,
        broll=broll,
        head=head,
        effect=effect_name,
        head_corner=head_corner,
        text_corner=text_corner,
        subscribe=use_sub,
        outro=outro_path,
    )


def _maybe_append_outro(
    main: Path,
    outro_dir: Path | None,
    *,
    rng: random.Random,
    on_progress: ProgressCb | None,
) -> Path | None:
    """Если папка аутро задана — дописать случайный клип в конец. Иначе no-op."""
    if outro_dir is None:
        return None
    if not outro_dir.is_dir():
        if on_progress:
            on_progress(1.0, f"Аутро: папка не найдена, пропускаю ({outro_dir})")
        return None
    clip = pick_outro(outro_dir, rng)
    if clip is None:
        if on_progress:
            on_progress(1.0, f"Аутро: нет видео в папке, пропускаю ({outro_dir})")
        return None
    if on_progress:
        on_progress(0.92, f"Аутро: {clip.name}")
    tmp = main.with_name(main.stem + ".tmp_outro" + main.suffix)
    try:
        _concat_outro(main, clip, tmp, on_progress=on_progress)
        tmp.replace(main)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    if on_progress:
        on_progress(1.0, "Аутро: готово")
    return clip


def _concat_outro(
    main: Path,
    outro: Path,
    output: Path,
    *,
    on_progress: ProgressCb | None,
) -> None:
    """Склеить main + outro (scale/crop как у футажей) в output."""
    ffmpeg = find_ffmpeg()
    main_dur = probe_duration(main)
    outro_dur = probe_duration(outro)
    total = main_dur + outro_dur
    w, h, fps = OUT_WIDTH, OUT_HEIGHT, OUT_FPS

    cmd: list[str] = [ffmpeg, "-y", "-i", str(main), "-i", str(outro)]
    if has_audio_stream(outro):
        filters = (
            f"[0:v]fps={fps},format=yuv420p,setsar=1[v0];"
            f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v1];"
            f"[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a0];"
            f"[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
        )
    else:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{outro_dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        filters = (
            f"[0:v]fps={fps},format=yuv420p,setsar=1[v0];"
            f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v1];"
            f"[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a0];"
            f"[2:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
        )

    cmd += [
        "-filter_complex",
        filters,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, total, on_progress)


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _parse_time(s: str) -> float | None:
    m = _TIME_RE.search(s)
    if not m:
        return None
    h, mi, sec = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(sec)


def _run_ffmpeg(
    cmd: list[str],
    total: float,
    on_progress: ProgressCb | None,
) -> None:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    err_lines: list[str] = []
    for line in proc.stderr:
        err_lines.append(line)
        if on_progress and total > 0:
            t = _parse_time(line)
            if t is not None:
                pct = max(0.0, min(0.99, t / total))
                on_progress(pct, f"Рендер {pct * 100:.0f}%")
    code = proc.wait()
    if code != 0:
        tail = "".join(err_lines[-40:])
        raise RuntimeError(f"ffmpeg failed ({code}):\n{tail}")
    if on_progress:
        on_progress(1.0, "Готово")
