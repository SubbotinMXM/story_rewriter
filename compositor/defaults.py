"""Константы и дефолты композера."""

from __future__ import annotations

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

CORNERS = ("tl", "tr", "bl", "br")

# 480p 16:9
OUT_WIDTH = 854
OUT_HEIGHT = 480
OUT_FPS = 30

BROLL_COUNT_MIN = 4
BROLL_COUNT_MAX = 5

HEAD_WIDTH_RATIO = 0.20
OVERLAY_MARGIN = 16

TEXT_FONTSIZE = 56  # 2× от исходных 28

VIDEO_PRESET = "veryfast"
VIDEO_CRF = 23
AUDIO_BITRATE = "192k"

# Анимация подписки (хромакей зелёного)
SUBSCRIBE_PATH = "/Users/mac/Desktop/AAA.mp4"
SUBSCRIBE_START_MIN = 7.0
SUBSCRIBE_START_MAX = 8.0
SUBSCRIBE_VOLUME = 0.8  # на 20% тише оригинала
SUBSCRIBE_CHROMA = "0x60FB01"  # реальный зелёный из AAA.mp4
SUBSCRIBE_SIMILARITY = 0.12
SUBSCRIBE_BLEND = 0.08
SUBSCRIBE_MAX_WIDTH_RATIO = 0.70

MAC_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
