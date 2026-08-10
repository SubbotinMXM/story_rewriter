"""PIL-хром слева для woman_random_portrait_v1 (текст НЕ в image-промпте).

Спрайты: assets/chrome/{flourish,divider}.png
(реконструкция по chrome_ref: яркий центр + линии; тонкие усы на рефе почти серые).
Лейаут — доли высоты с woman_random_portrait_v1_chrome_ref.png.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_ROOT = Path(__file__).resolve().parent.parent
_FONTS = _ROOT / "assets" / "fonts"
_CHROME = _ROOT / "assets" / "chrome"

# #FFD740
GOLD = (255, 215, 64, 255)
WHITE = (255, 255, 255, 255)
SHADOW = (0, 0, 0, 170)

_FONT_CANDIDATES_BLACK = [
    _FONTS / "Montserrat-Black.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    Path("/Library/Fonts/Arial Black.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
_FONT_CANDIDATES_BOLD = [
    _FONTS / "Montserrat-Bold.ttf",
    _FONTS / "Montserrat-Black.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]

TOP_LINE = "ЛУЧШАЯ ИСТОРИЯ"
MID_LINES = ("РАССКАЗ КОТОРЫЙ", "ТРОГАЕТ ДО СЛЁЗ")
BOTTOM_LINE = "АУДИО-РАССКАЗ"

# Доли кадра с chrome_ref 688×386 (центры строк / орнаментов).
_CX_FRAC = 0.274  # ось текста
_ORNAMENT_W_FRAC = 0.45  # ширина спрайтов относительно кадра
_TITLE_MAX_W_FRAC = 0.50
_Y = {
    "top": 0.074,
    "flourish": 0.197,
    "title1": 0.329,  # «Я ОКАЗАЛАСЬ»
    "title2": 0.523,  # «СИЛЬНЕЕ» (крупнее)
    "title_one": 0.42,  # одна строка — середина блока
    "divider": 0.671,
    "mid1": 0.772,
    "mid2": 0.855,
    "bot": 0.943,
}


def _truetype(candidates: list[Path], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_text_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    *,
    font,
    fill: tuple[int, int, int, int],
    shadow: int = 3,
    tracking: float = 0.0,
) -> None:
    """xy — центр строки (Pillow anchor=mm)."""
    x, y = xy
    if tracking and len(text) > 1:
        widths = [_text_size(draw, ch, font)[0] for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        cursor = x - total / 2
        for ch, cw in zip(text, widths):
            ch_cx = cursor + cw / 2
            for dx in range(-shadow, shadow + 1):
                for dy in range(-shadow, shadow + 1):
                    if dx == 0 and dy == 0:
                        continue
                    if abs(dx) + abs(dy) > shadow + 1:
                        continue
                    draw.text((ch_cx + dx, y + dy), ch, font=font, fill=SHADOW, anchor="mm")
            draw.text((ch_cx, y), ch, font=font, fill=fill, anchor="mm")
            cursor += cw + tracking
        return

    for dx in range(-shadow, shadow + 1):
        for dy in range(-shadow, shadow + 1):
            if dx == 0 and dy == 0:
                continue
            if abs(dx) + abs(dy) > shadow + 1:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=SHADOW, anchor="mm")
    draw.text((x, y), text, font=font, fill=fill, anchor="mm")


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    cx: float,
    y_center: float,
    text: str,
    *,
    font,
    fill: tuple[int, int, int, int],
    shadow: int = 3,
    tracking: float = 0.0,
) -> None:
    _draw_text_shadow(
        draw,
        (cx, y_center),
        text,
        font=font,
        fill=fill,
        shadow=shadow,
        tracking=tracking,
    )


def wrap_title(title: str, *, font, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    """1–2 строки CAPS; длинные — пополам по словам."""
    words = [w for w in (title or "").upper().split() if w]
    if not words:
        return [""]
    one = " ".join(words)
    if _text_size(draw, one, font)[0] <= max_width or len(words) == 1:
        return [one]

    best: list[str] | None = None
    best_score = 1e18
    for i in range(1, len(words)):
        a = " ".join(words[:i])
        b = " ".join(words[i:])
        wa, _ = _text_size(draw, a, font)
        wb, _ = _text_size(draw, b, font)
        if wa > max_width or wb > max_width:
            continue
        score = abs(wa - wb) + max(wa, wb) * 0.01
        if score < best_score:
            best_score = score
            best = [a, b]
    if best:
        return best

    line1: list[str] = []
    for w in words:
        trial = " ".join(line1 + [w])
        if line1 and _text_size(draw, trial, font)[0] > max_width:
            break
        line1.append(w)
    if not line1:
        line1 = [words[0]]
    rest = words[len(line1) :]
    if not rest:
        return [" ".join(line1)]
    return [" ".join(line1), " ".join(rest)]


def _fit_title_fonts(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int,
    *,
    h: int,
) -> tuple[list[ImageFont.ImageFont], list[str]]:
    """1 строка — крупный шрифт; 2 строки — вторая ~1.25× (как на рефе)."""
    size_hi = max(56, int(h * 0.145))
    size_lo = max(28, int(h * 0.055))
    probe = _truetype(_FONT_CANDIDATES_BLACK, size_hi)
    lines = wrap_title(title, font=probe, draw=draw, max_width=max_width)

    if len(lines) <= 1:
        for size in range(size_hi, size_lo - 1, -2):
            font = _truetype(_FONT_CANDIDATES_BLACK, size)
            lines = wrap_title(title, font=font, draw=draw, max_width=max_width)
            if len(lines) == 1 and _text_size(draw, lines[0], font)[0] <= max_width:
                return [font], lines
        font = _truetype(_FONT_CANDIDATES_BLACK, size_lo)
        return [font], wrap_title(title, font=font, draw=draw, max_width=max_width)

    # две строки: подбираем size2 (крупная), size1 ≈ 0.72× (как на рефе)
    size2_hi = max(68, int(h * 0.165))
    for size2 in range(size2_hi, size_lo - 1, -2):
        size1 = max(size_lo, int(size2 * 0.72))
        f1 = _truetype(_FONT_CANDIDATES_BLACK, size1)
        f2 = _truetype(_FONT_CANDIDATES_BLACK, size2)
        # переразбивка по более узкой из двух при size1
        lines = wrap_title(title, font=f1, draw=draw, max_width=max_width)
        if len(lines) == 1:
            lines = wrap_title(title, font=f2, draw=draw, max_width=max_width)
        if len(lines) != 2:
            continue
        if (
            _text_size(draw, lines[0], f1)[0] <= max_width
            and _text_size(draw, lines[1], f2)[0] <= max_width
        ):
            return [f1, f2], lines

    f1 = _truetype(_FONT_CANDIDATES_BLACK, size_lo)
    f2 = _truetype(_FONT_CANDIDATES_BLACK, max(size_lo, int(size_lo * 1.2)))
    lines = wrap_title(title, font=f1, draw=draw, max_width=max_width)
    if len(lines) == 1:
        return [f2], lines
    return [f1, f2], lines


@lru_cache(maxsize=4)
def _load_sprite(name: str) -> Image.Image:
    path = _CHROME / f"{name}.png"
    if not path.is_file():
        raise FileNotFoundError(f"chrome sprite missing: {path}")
    return Image.open(path).convert("RGBA")


def _paste_sprite(
    base: Image.Image,
    sprite: Image.Image,
    cx: float,
    y_center: float,
    target_w: int,
) -> None:
    if sprite.width <= 0 or sprite.height <= 0:
        return
    scale = target_w / sprite.width
    nw = max(1, int(sprite.width * scale))
    nh = max(1, int(sprite.height * scale))
    sp = sprite.resize((nw, nh), Image.LANCZOS)
    # лёгкая тень под орнаментом
    shadow = Image.new("RGBA", sp.size, (0, 0, 0, 0))
    alpha = sp.split()[3].point(lambda a: min(120, a // 2))
    shadow.putalpha(alpha)
    x = int(cx - nw / 2)
    y = int(y_center - nh / 2)
    base.alpha_composite(shadow, (x + 2, y + 2))
    base.alpha_composite(sp, (x, y))


def render_portrait_chrome(image_path: Path, title: str) -> Path:
    """Нарисовать фиксированный хром + переменный заголовок в левой зоне 16:9 PNG."""
    path = Path(image_path)
    with Image.open(path) as im:
        img = im.convert("RGBA")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        cx = w * _CX_FRAC
        max_title_w = int(w * _TITLE_MAX_W_FRAC)
        ornament_w = int(w * _ORNAMENT_W_FRAC)

        font_top = _truetype(_FONT_CANDIDATES_BOLD, max(20, int(h * 0.055)))
        font_mid = _truetype(_FONT_CANDIDATES_BOLD, max(18, int(h * 0.044)))
        font_bot = _truetype(_FONT_CANDIDATES_BOLD, max(18, int(h * 0.044)))

        title_fonts, title_lines = _fit_title_fonts(
            draw, title.strip(), max_title_w, h=h,
        )

        tracking_top = max(0.5, h * 0.0018)
        tracking_mid = max(0.3, h * 0.0012)

        _draw_centered(
            draw, cx, h * _Y["top"], TOP_LINE,
            font=font_top, fill=WHITE, shadow=2, tracking=tracking_top,
        )

        try:
            _paste_sprite(img, _load_sprite("flourish"), cx, h * _Y["flourish"], ornament_w)
        except FileNotFoundError:
            pass

        if len(title_lines) == 1:
            _draw_centered(
                draw, cx, h * _Y["title_one"], title_lines[0],
                font=title_fonts[0], fill=GOLD, shadow=4,
            )
        else:
            ys = (_Y["title1"], _Y["title2"])
            for i, ln in enumerate(title_lines[:2]):
                font = title_fonts[i] if i < len(title_fonts) else title_fonts[-1]
                _draw_centered(
                    draw, cx, h * ys[i], ln,
                    font=font, fill=GOLD, shadow=4,
                )

        try:
            _paste_sprite(img, _load_sprite("divider"), cx, h * _Y["divider"], ornament_w)
        except FileNotFoundError:
            pass

        _draw_centered(
            draw, cx, h * _Y["mid1"], MID_LINES[0],
            font=font_mid, fill=WHITE, shadow=2, tracking=tracking_mid,
        )
        _draw_centered(
            draw, cx, h * _Y["mid2"], MID_LINES[1],
            font=font_mid, fill=WHITE, shadow=2, tracking=tracking_mid,
        )
        _draw_centered(
            draw, cx, h * _Y["bot"], BOTTOM_LINE,
            font=font_bot, fill=GOLD, shadow=2, tracking=tracking_mid,
        )

        out = img.convert("RGB")
        out.save(path, format="PNG")
    return path
