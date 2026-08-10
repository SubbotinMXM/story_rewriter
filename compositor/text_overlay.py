"""Рендер текста с обводкой в PNG (без libfreetype/drawtext)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from compositor.defaults import (
    MAC_FONT_CANDIDATES,
    OUT_HEIGHT,
    OUT_WIDTH,
    OVERLAY_MARGIN,
    TEXT_FONTSIZE,
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in MAC_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_text_png(text: str, path: Path, corner: str) -> Path:
    """Полный кадр RGBA с текстом в углу — для overlay на видео."""
    img = Image.new("RGBA", (OUT_WIDTH, OUT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(TEXT_FONTSIZE)
    body = text.strip() or " "
    bbox = draw.multiline_textbbox((0, 0), body, font=font, spacing=6)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    m = OVERLAY_MARGIN
    positions = {
        "tl": (m, m),
        "tr": (OUT_WIDTH - tw - m, m),
        "bl": (m, OUT_HEIGHT - th - m),
        "br": (OUT_WIDTH - tw - m, OUT_HEIGHT - th - m),
    }
    x, y = positions.get(corner, positions["tl"])
    outline = 3
    for dx in range(-outline, outline + 1):
        for dy in range(-outline, outline + 1):
            if dx == 0 and dy == 0:
                continue
            draw.multiline_text(
                (x + dx, y + dy), body, font=font, fill=(0, 0, 0, 255), spacing=6
            )
    draw.multiline_text((x, y), body, font=font, fill=(255, 255, 255, 255), spacing=6)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path
