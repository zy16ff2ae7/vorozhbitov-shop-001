#!/usr/bin/env python3
"""Отрисовка чата Telegram для видео: пузыри, кнопки, альбомы, правка на месте.

Движок знает только то, что присылает ``bot.py``: разметку ``inline_keyboard``,
обычную клавиатуру, альбомы, счета и HTML-теги ``<b>``, ``<i>``, ``<code>``.
Знаки из подписей кнопок (📐 ⏳ 🏠) рисуются своим монохромным набором: цветных
шрифтов эмодзи в песочнице нет, а «квадратики» вместо знаков портят кадр.

Отдельно этот файл запускается как проверка вида — рисует один кадр с примерами
всех типов сообщений:

    python3 tools/video/chat_render.py --sample /tmp/sample.png
"""
from __future__ import annotations

import argparse
import html
import math
import re
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE_DIR = Path(__file__).resolve().parents[2]
FONT_DIR = BASE_DIR / "tools" / "video" / "fonts"
DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
ASSETS = BASE_DIR / "miniapp" / "assets"

W, H = 1080, 1920
FPS = 30

# Ночная тема Telegram, чуть холоднее — под чёрно-стальную айдентику выпуска.
BG_TOP = (17, 23, 30)
BG_BOTTOM = (9, 12, 16)
HEADER_BG = (23, 30, 38)
HAIRLINE = (255, 255, 255, 22)
BUBBLE_IN = (30, 38, 48)
BUBBLE_OUT = (43, 82, 120)
TEXT = (238, 243, 248)
TEXT_DIM = (150, 166, 181)
TEXT_TIME = (133, 149, 164)
BUTTON_TEXT = (146, 197, 240)
BUTTON_BG = (255, 255, 255, 12)
KEY_BG = (36, 45, 56)
KEYBOARD_BG = (22, 28, 36)
ACCENT = (146, 197, 240)

HEADER_H = 158
MARGIN_X = 22
BUBBLE_MAX_W = 900
PAD = 28
GAP = 14
RADIUS = 26
BUTTON_H = 92
BODY_SIZE = 42
LINE_H = 58
BUTTON_SIZE = 39
CODE_SIZE = 38


# --------------------------------------------------------------------- шрифты

class Fonts:
    """Пара гарнитур: Oswald для заголовков и кнопок, DejaVu для текста."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def _load(self, path: Path, size: int) -> ImageFont.FreeTypeFont:
        key = (str(path), size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(str(path), size)
        return self._cache[key]

    def body(self, size: int = BODY_SIZE, bold: bool = False) -> ImageFont.FreeTypeFont:
        return self._load(DEJAVU / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)

    def brand(self, size: int = BUTTON_SIZE, bold: bool = False) -> ImageFont.FreeTypeFont:
        name = "Oswald-Bold.ttf" if bold and (FONT_DIR / "Oswald-Bold.ttf").exists() else "Oswald-Regular.ttf"
        return self._load(FONT_DIR / name, size)

    def display(self, size: int) -> ImageFont.FreeTypeFont:
        return self._load(FONT_DIR / "RuslanDisplay.ttf", size)


FONTS = Fonts()


# ----------------------------------------------------------------------- знаки

def _norm(box: tuple[float, float, float, float]) -> Callable[[float, float], tuple[float, float]]:
    left, top, right, bottom = box

    def point(x: float, y: float) -> tuple[float, float]:
        return left + x * (right - left), top + y * (bottom - top)

    return point


class IconPen:
    """Рисует знак в единичном квадрате: координаты 0..1, толщина пропорциональна."""

    def __init__(self, draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float],
                 color: tuple[int, ...], weight: float = 0.085):
        self.d = draw
        self.p = _norm(box)
        self.color = color
        self.w = weight * min(box[2] - box[0], box[3] - box[1])

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float | None = None) -> None:
        self.d.line([self.p(x1, y1), self.p(x2, y2)], fill=self.color,
                    width=int(round(width or self.w)), joint="curve")

    def poly(self, points: Sequence[tuple[float, float]], width: float | None = None) -> None:
        self.d.line([self.p(x, y) for x, y in points], fill=self.color,
                    width=int(round(width or self.w)), joint="curve")

    def circle(self, cx: float, cy: float, r: float, width: float | None = None,
               fill: tuple[int, ...] | None = None) -> None:
        left, top = self.p(cx - r, cy - r)
        right, bottom = self.p(cx + r, cy + r)
        if fill:
            self.d.ellipse([left, top, right, bottom], fill=fill)
        else:
            self.d.ellipse([left, top, right, bottom], outline=self.color,
                           width=int(round(width or self.w)))

    def rect(self, x1: float, y1: float, x2: float, y2: float, r: float = 0.0,
             width: float | None = None, fill: tuple[int, ...] | None = None) -> None:
        left, top = self.p(x1, y1)
        right, bottom = self.p(x2, y2)
        radius = r * min(right - left, bottom - top)
        if fill:
            self.d.rounded_rectangle([left, top, right, bottom], radius=radius, fill=fill)
        else:
            self.d.rounded_rectangle([left, top, right, bottom], radius=radius, outline=self.color,
                                     width=int(round(width or self.w)))

    def arc(self, cx: float, cy: float, r: float, start: float, end: float,
            width: float | None = None) -> None:
        left, top = self.p(cx - r, cy - r)
        right, bottom = self.p(cx + r, cy + r)
        self.d.arc([left, top, right, bottom], start=start, end=end, fill=self.color,
                   width=int(round(width or self.w)))


ICONS: dict[int, Callable[[IconPen], None]] = {}


def icon(code: int):
    def wrap(fn: Callable[[IconPen], None]):
        ICONS[code] = fn
        return fn
    return wrap


@icon(0x1F3E0)  # 🏠 главная
def _(p: IconPen) -> None:
    p.poly([(0.12, 0.52), (0.5, 0.16), (0.88, 0.52)])
    p.poly([(0.24, 0.48), (0.24, 0.86), (0.76, 0.86), (0.76, 0.48)])
    p.rect(0.42, 0.62, 0.58, 0.86)


@icon(0x23F3)  # ⏳ жду размер
def _(p: IconPen) -> None:
    p.line(0.26, 0.14, 0.74, 0.14)
    p.line(0.26, 0.86, 0.74, 0.86)
    p.poly([(0.3, 0.16), (0.3, 0.36), (0.5, 0.52), (0.7, 0.36), (0.7, 0.16)])
    p.poly([(0.3, 0.84), (0.3, 0.66), (0.5, 0.52), (0.7, 0.66), (0.7, 0.84)])


@icon(0x1F4D0)  # 📐 подобрать размер
def _(p: IconPen) -> None:
    p.poly([(0.16, 0.84), (0.16, 0.2), (0.84, 0.84), (0.16, 0.84)])
    for step in (0.32, 0.46, 0.6):
        p.line(0.16, step, 0.26, step, width=p.w * 0.8)


@icon(0x1F4CF)  # 📏 замеры
def _(p: IconPen) -> None:
    p.rect(0.12, 0.36, 0.88, 0.64, r=0.12)
    for x in (0.28, 0.42, 0.56, 0.7):
        p.line(x, 0.36, x, 0.48, width=p.w * 0.8)


@icon(0x1F4E6)  # 📦 покупки
def _(p: IconPen) -> None:
    p.poly([(0.5, 0.14), (0.86, 0.32), (0.86, 0.7), (0.5, 0.88), (0.14, 0.7), (0.14, 0.32), (0.5, 0.14)])
    p.line(0.14, 0.32, 0.5, 0.5)
    p.line(0.86, 0.32, 0.5, 0.5)
    p.line(0.5, 0.5, 0.5, 0.88)


@icon(0x1F6CD)  # 🛍 новая вещь
def _(p: IconPen) -> None:
    p.poly([(0.2, 0.34), (0.28, 0.86), (0.62, 0.86), (0.68, 0.34), (0.2, 0.34)])
    p.arc(0.36, 0.34, 0.1, 180, 360)
    p.poly([(0.6, 0.4), (0.78, 0.4), (0.84, 0.86), (0.66, 0.86)], width=p.w * 0.8)


@icon(0x1F4CA)  # 📊 сводка
def _(p: IconPen) -> None:
    p.line(0.16, 0.86, 0.88, 0.86)
    p.rect(0.24, 0.54, 0.38, 0.82, r=0.08, fill=p.color)
    p.rect(0.46, 0.36, 0.6, 0.82, r=0.08, fill=p.color)
    p.rect(0.68, 0.2, 0.82, 0.82, r=0.08, fill=p.color)


@icon(0x1F4B3)  # 💳 оплата
def _(p: IconPen) -> None:
    p.rect(0.1, 0.26, 0.9, 0.74, r=0.12)
    p.line(0.1, 0.44, 0.9, 0.44, width=p.w * 1.2)
    p.line(0.22, 0.62, 0.42, 0.62, width=p.w * 0.9)


@icon(0x1F464)  # 👤 режим покупателя
def _(p: IconPen) -> None:
    p.circle(0.5, 0.34, 0.16)
    p.arc(0.5, 0.94, 0.34, 180, 360)


@icon(0x1F4E4)  # 📤 экспорт
def _(p: IconPen) -> None:
    p.poly([(0.16, 0.5), (0.16, 0.84), (0.84, 0.84), (0.84, 0.5)])
    p.line(0.5, 0.68, 0.5, 0.16)
    p.poly([(0.34, 0.32), (0.5, 0.16), (0.66, 0.32)])


@icon(0x1F504)  # 🔄 перечитать каталог
def _(p: IconPen) -> None:
    p.arc(0.5, 0.5, 0.34, 40, 300)
    p.poly([(0.72, 0.16), (0.86, 0.3), (0.68, 0.36)], width=p.w * 0.9)
    p.poly([(0.28, 0.84), (0.14, 0.7), (0.32, 0.64)], width=p.w * 0.9)


@icon(0x1F3C6)  # 🏆 топ рефералов
def _(p: IconPen) -> None:
    p.poly([(0.3, 0.16), (0.7, 0.16), (0.66, 0.44), (0.5, 0.56), (0.34, 0.44), (0.3, 0.16)])
    p.arc(0.24, 0.3, 0.12, 90, 270)
    p.arc(0.76, 0.3, 0.12, 270, 90)
    p.line(0.5, 0.56, 0.5, 0.74)
    p.line(0.34, 0.84, 0.66, 0.84)


@icon(0x1F6E0)  # 🛠 управление
def _(p: IconPen) -> None:
    p.line(0.16, 0.84, 0.52, 0.48)
    p.arc(0.66, 0.34, 0.2, 110, 400)
    p.line(0.52, 0.48, 0.6, 0.56, width=p.w * 1.4)


@icon(0x1F4F7)  # 📷 фото
def _(p: IconPen) -> None:
    p.rect(0.1, 0.28, 0.9, 0.78, r=0.1)
    p.poly([(0.36, 0.28), (0.42, 0.18), (0.58, 0.18), (0.64, 0.28)])
    p.circle(0.5, 0.54, 0.15)


@icon(0x1F3AC)  # 🎬 фильм выпуска
def _(p: IconPen) -> None:
    p.rect(0.1, 0.32, 0.9, 0.84, r=0.06)
    p.line(0.1, 0.5, 0.9, 0.5)
    p.poly([(0.24, 0.32), (0.18, 0.5)])
    p.poly([(0.5, 0.32), (0.44, 0.5)])
    p.poly([(0.76, 0.32), (0.7, 0.5)])
    p.poly([(0.2, 0.32), (0.34, 0.16), (0.8, 0.16), (0.66, 0.32)], width=p.w * 0.8)


@icon(0x1F4AC)  # 💬 вопрос менеджеру
def _(p: IconPen) -> None:
    p.rect(0.12, 0.18, 0.88, 0.66, r=0.16)
    p.poly([(0.32, 0.64), (0.3, 0.86), (0.52, 0.66)])


@icon(0x1F4E3)  # 📣 рассылка
def _(p: IconPen) -> None:
    p.poly([(0.16, 0.44), (0.42, 0.44), (0.74, 0.2), (0.74, 0.76), (0.42, 0.56), (0.16, 0.56), (0.16, 0.44)])
    p.poly([(0.4, 0.58), (0.44, 0.84), (0.56, 0.84), (0.5, 0.6)], width=p.w * 0.8)


@icon(0x1F514)  # 🔔 напомнить
def _(p: IconPen) -> None:
    p.arc(0.5, 0.5, 0.3, 180, 360)
    p.poly([(0.2, 0.5), (0.2, 0.68), (0.14, 0.76), (0.86, 0.76), (0.8, 0.68), (0.8, 0.5)])
    p.arc(0.5, 0.8, 0.09, 0, 180)


@icon(0x1F517)  # 🔗 ссылка
def _(p: IconPen) -> None:
    p.arc(0.36, 0.5, 0.2, 90, 270)
    p.arc(0.64, 0.5, 0.2, 270, 90)
    p.line(0.36, 0.3, 0.64, 0.3)
    p.line(0.36, 0.7, 0.64, 0.7)


@icon(0x1F4F1)  # 📱 номер
def _(p: IconPen) -> None:
    p.rect(0.3, 0.08, 0.7, 0.92, r=0.16)
    p.line(0.42, 0.18, 0.58, 0.18, width=p.w * 0.8)
    p.circle(0.5, 0.82, 0.05, fill=p.color)


@icon(0x1F69A)  # 🚚 доставка
def _(p: IconPen) -> None:
    p.rect(0.08, 0.3, 0.56, 0.7, r=0.06)
    p.poly([(0.56, 0.42), (0.76, 0.42), (0.9, 0.58), (0.9, 0.7), (0.56, 0.7)])
    p.circle(0.3, 0.78, 0.09)
    p.circle(0.74, 0.78, 0.09)


@icon(0x1F9FE)  # 🧾 чек
def _(p: IconPen) -> None:
    p.poly([(0.24, 0.12), (0.76, 0.12), (0.76, 0.88), (0.66, 0.78), (0.56, 0.88),
            (0.46, 0.78), (0.36, 0.88), (0.24, 0.78), (0.24, 0.12)])
    p.line(0.36, 0.34, 0.64, 0.34, width=p.w * 0.8)
    p.line(0.36, 0.52, 0.64, 0.52, width=p.w * 0.8)


@icon(0x1F3F7)  # 🏷 ярлык
def _(p: IconPen) -> None:
    p.poly([(0.14, 0.5), (0.44, 0.18), (0.84, 0.18), (0.84, 0.58), (0.52, 0.88), (0.14, 0.5)])
    p.circle(0.68, 0.36, 0.07, fill=p.color)


@icon(0x26A1)  # ⚡ быстро
def _(p: IconPen) -> None:
    p.poly([(0.58, 0.1), (0.28, 0.54), (0.48, 0.54), (0.4, 0.9), (0.74, 0.42), (0.52, 0.42), (0.58, 0.1)])


@icon(0x2705)  # ✅ готово
def _(p: IconPen) -> None:
    p.circle(0.5, 0.5, 0.38)
    p.poly([(0.32, 0.52), (0.45, 0.66), (0.7, 0.36)])


@icon(0x2753)  # ❓ вопрос
def _(p: IconPen) -> None:
    p.circle(0.5, 0.5, 0.38)
    p.arc(0.5, 0.4, 0.13, 200, 340)
    p.line(0.5, 0.52, 0.5, 0.6)
    p.circle(0.5, 0.72, 0.035, fill=p.color)


@icon(0x2139)  # ℹ подробнее
def _(p: IconPen) -> None:
    p.circle(0.5, 0.5, 0.38)
    p.line(0.5, 0.44, 0.5, 0.72)
    p.circle(0.5, 0.3, 0.04, fill=p.color)


@icon(0x1F9F5)  # 🧵 состав
def _(p: IconPen) -> None:
    p.rect(0.28, 0.16, 0.72, 0.84, r=0.14)
    p.line(0.28, 0.36, 0.72, 0.36, width=p.w * 0.8)
    p.line(0.28, 0.64, 0.72, 0.64, width=p.w * 0.8)


@icon(0x2702)  # ✂ крой
def _(p: IconPen) -> None:
    p.circle(0.28, 0.74, 0.11)
    p.circle(0.28, 0.26, 0.11)
    p.line(0.36, 0.66, 0.84, 0.2)
    p.line(0.36, 0.34, 0.84, 0.8)


TEXT_ICONS = {0x00B7, 0x2022, 0x2014, 0x2013, 0x2116, 0x2122, 0x00A9, 0x00AE, 0x221E}


@icon(0x2190)  # back
def _(p: IconPen) -> None:
    p.line(0.86, 0.5, 0.18, 0.5)
    p.poly([(0.4, 0.26), (0.16, 0.5), (0.4, 0.74)])


@icon(0x2192)  # forward
def _(p: IconPen) -> None:
    p.line(0.14, 0.5, 0.82, 0.5)
    p.poly([(0.6, 0.26), (0.84, 0.5), (0.6, 0.74)])


@icon(0x2197)  # outward
def _(p: IconPen) -> None:
    p.line(0.22, 0.78, 0.78, 0.22)
    p.poly([(0.46, 0.2), (0.8, 0.2), (0.8, 0.54)])


@icon(0x2193)  # down
def _(p: IconPen) -> None:
    p.line(0.5, 0.14, 0.5, 0.82)
    p.poly([(0.26, 0.6), (0.5, 0.84), (0.74, 0.6)])


@icon(0x2716)  # cross
def _(p: IconPen) -> None:
    p.line(0.24, 0.24, 0.76, 0.76)
    p.line(0.76, 0.24, 0.24, 0.76)


def is_icon_char(ch: str) -> bool:
    code = ord(ch)
    return code > 0x2100 and code not in TEXT_ICONS


def draw_icon(draw: ImageDraw.ImageDraw, ch: str | int, box: tuple[float, float, float, float],
              color: tuple[int, ...]) -> bool:
    """Нарисовать знак в ``box``. Возвращает False, если знака в наборе нет."""
    fn = ICONS.get(ch if isinstance(ch, int) else ord(ch))
    if not fn:
        return False
    fn(IconPen(draw, box, color))
    return True


# ----------------------------------------------------------------- HTML бота

TAG_RE = re.compile(r"<(/?)(b|i|code|a)(?:\s[^>]*)?>", re.IGNORECASE)
ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|\w+);")


def unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return html.unescape(match.group(0))
        except Exception:
            return match.group(0)
    return ENTITY_RE.sub(repl, value)


def segments(text: str) -> list[tuple[str, str]]:
    """Разобрать HTML бота на куски ``(текст, стиль)``."""
    out: list[tuple[str, str]] = []
    stack: list[str] = []
    pos = 0
    for match in TAG_RE.finditer(text or ""):
        if match.start() > pos:
            out.append((unescape(text[pos:match.start()]), "".join(stack)))
        closing, tag = match.group(1), match.group(2).lower()
        if closing:
            if tag in stack:
                stack.remove(tag)
        elif tag in ("b", "i", "code"):
            stack.append(tag)
        pos = match.end()
    if pos < len(text or ""):
        out.append((unescape(text[pos:]), "".join(stack)))
    return [(chunk, style) for chunk, style in out if chunk]


def font_for(style: str, size: int = BODY_SIZE) -> ImageFont.FreeTypeFont:
    if "code" in style:
        return FONTS.body(size - 4)
    return FONTS.body(size, bold="b" in style)


def color_for(style: str) -> tuple[int, ...]:
    if "code" in style:
        return (196, 214, 230)
    return TEXT


class Line:
    __slots__ = ("parts", "width", "height", "icon_box")

    def __init__(self) -> None:
        # Части строки: (текст, шрифт, цвет); шрифт None — знак из набора ICONS.
        self.parts: list[tuple[str, ImageFont.FreeTypeFont | None, tuple[int, ...]]] = []
        self.width = 0
        self.height = LINE_H
        self.icon_box = BODY_SIZE + 6


def wrap_segments(chunks: Iterable[tuple[str, str]], max_width: int,
                  size: int = BODY_SIZE, line_height: int = LINE_H) -> list[Line]:
    """Перенос по словам с учётом стилей и знаков вместо эмодзи."""
    lines: list[Line] = []
    current = Line()
    current.height = line_height
    want_space = False

    def flush() -> None:
        nonlocal current, want_space
        if current.parts:
            lines.append(current)
        current = Line()
        current.height = line_height
        current.icon_box = size + 6
        want_space = False

    def add_space(font: ImageFont.FreeTypeFont, color: tuple[int, ...]) -> None:
        nonlocal want_space
        if not current.parts:
            want_space = False
            return
        width = font.getlength(" ")
        if current.width + width > max_width:
            flush()
        else:
            current.parts.append((" ", font, color))
            current.width += width
        want_space = False

    for chunk, style in chunks:
        font = font_for(style, size)
        color = color_for(style)
        for token in re.split(r"(\n| )", chunk):
            if not token:
                continue
            if token == "\n":
                flush()
                continue
            if token == " ":
                want_space = True
                continue
            if want_space:
                add_space(font, color)
            plain = "".join(ch for ch in token if not is_icon_char(ch))
            icons = [(ch, position) for position, ch in enumerate(token) if is_icon_char(ch)]
            width = font.getlength(plain) if plain else 0.0
            width += len(icons) * (size + 6)
            if current.width + width > max_width and current.parts:
                flush()
            cursor = 0
            for ch, position in icons:
                before = token[cursor:position]
                if before:
                    current.parts.append((before, font, color))
                    current.width += font.getlength(before)
                current.parts.append((ch, None, color))
                current.width += size + 6
                cursor = position + 1
            tail = token[cursor:]
            if tail:
                current.parts.append((tail, font, color))
                current.width += font.getlength(tail)
    flush()
    return lines


def wrap_text(text: str, max_width: int, size: int = BODY_SIZE,
              line_height: int = LINE_H) -> list[Line]:
    """Перенос с сохранением переводов строк из сообщения бота."""
    lines: list[Line] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph.strip():
            blank = Line()
            blank.height = int(line_height * 0.62)
            lines.append(blank)
            continue
        lines.extend(wrap_segments(segments(paragraph), max_width, size, line_height))
    return lines


def draw_lines(draw: ImageDraw.ImageDraw, lines: Sequence[Line], x: int, y: int,
               icon_color: tuple[int, ...] = TEXT_DIM) -> int:
    cursor = y
    for line in lines:
        pen_x = x
        for chunk, font, color in line.parts:
            if font is None:
                box = (pen_x, cursor + (line.height - line.icon_box) // 2 - 4,
                       pen_x + line.icon_box, cursor + (line.height - line.icon_box) // 2 - 4 + line.icon_box)
                if not draw_icon(draw, chunk, box, icon_color):
                    draw.rounded_rectangle(box, radius=line.icon_box // 5, outline=icon_color, width=2)
                pen_x += line.icon_box
                continue
            draw.text((pen_x, cursor), chunk, font=font, fill=color)
            pen_x += int(font.getlength(chunk))
        cursor += line.height
    return cursor


def text_height(lines: Sequence[Line]) -> int:
    return sum(line.height for line in lines)


# ------------------------------------------------------------- кнопки и клавиши

def button_rows(markup: dict[str, Any] | None) -> list[list[dict[str, Any]]]:
    return (markup or {}).get("inline_keyboard") or []


def reply_rows(markup: dict[str, Any] | None) -> list[list[dict[str, Any]]]:
    keyboard = (markup or {}).get("keyboard") or []
    rows: list[list[dict[str, Any]]] = []
    for row in keyboard:
        keys = row if isinstance(row, list) else [row]
        rows.append([key for key in keys if isinstance(key, dict)])
    return rows


def layout_buttons(rows: list[list[dict[str, Any]]], width: int) -> list[list[dict[str, Any]]]:
    """Ширины кнопок в ряду — как в Telegram: по длине подписи, но не мельче трети."""
    laid: list[list[dict[str, Any]]] = []
    for row in rows:
        labels = [str(button.get("text", "")) for button in row]
        if not labels:
            continue
        font = FONTS.brand(BUTTON_SIZE)
        weights = [max(font.getlength(strip_icons(label)) + 34, width * 0.24) for label in labels]
        total = sum(weights)
        cells: list[dict[str, Any]] = []
        cursor = 0
        for index, button in enumerate(row):
            if index == len(row) - 1:
                cell_w = width - cursor
            else:
                cell_w = int(round(width * weights[index] / total))
            cells.append({**button, "label": labels[index], "x": cursor, "w": cell_w})
            cursor += cell_w
        laid.append(cells)
    return laid


def strip_icons(label: str) -> str:
    return "".join(ch for ch in label if not is_icon_char(ch)).strip()


# --------------------------------------------------------------------- слои

def rounded(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Обрезать по центру и растянуть на весь размер — как фото в пузыре."""
    target_w, target_h = size
    if not image.width or not image.height:
        return Image.new("RGB", (target_w, target_h), (18, 22, 28))
    ratio = max(target_w / image.width, target_h / image.height)
    scaled = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                          Image.LANCZOS)
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


_IMAGES: dict[str, Image.Image] = {}


def load_image(path: str | Path) -> Image.Image | None:
    """Открыть кадр: файл, путь из каталога или ссылка на собственную витрину.

    Бот отдаёт альбомы ссылками на веб-витрину (Telegram тянет их по HTTPS),
    но в ролике те же кадры лежат рядом в ``miniapp/assets`` — берём их оттуда.
    """
    key = str(path)
    if key in _IMAGES:
        return _IMAGES[key]
    candidates: list[Path] = []
    if key.startswith(("http://", "https://")):
        relative = urlparse(key).path.lstrip("/")
        candidates.extend([BASE_DIR / "miniapp" / relative, BASE_DIR / relative])
    else:
        candidate = Path(key)
        candidates.append(candidate if candidate.is_absolute() else BASE_DIR / "miniapp" / key)
        candidates.append(Path(key))
    for candidate in candidates:
        try:
            image = Image.open(candidate).convert("RGB")
        except Exception:
            continue
        _IMAGES[key] = image
        return image
    return None


class Layer:
    """Готовый пузырь сообщения: картинка, высота и прямоугольники кнопок."""

    def __init__(self, image: Image.Image, height: int, buttons: list[tuple[int, int, int, int]] = (),
                 width: int = 0):
        self.image = image
        self.height = height
        self.width = width or image.width
        self.buttons = list(buttons)


def render_text_bubble(text: str, markup: dict[str, Any] | None, outgoing: bool,
                       clock: str = "", max_w: int = BUBBLE_MAX_W) -> Layer:
    rows = layout_buttons(button_rows(markup), max_w - 2 * 10) if button_rows(markup) else []
    inner_w = max_w - 2 * PAD if rows else max_w - 2 * PAD
    lines = wrap_text(text, inner_w)
    body_h = text_height(lines)
    height = PAD + max(body_h, 8) + (34 if clock else 8) + PAD // 2
    if rows:
        height += len(rows) * BUTTON_H + 12
    width = max_w
    if not rows and lines:
        width = min(max_w, max(int(max(line.width for line in lines)) + 2 * PAD, 190))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=RADIUS,
                           fill=BUBBLE_OUT if outgoing else BUBBLE_IN)
    draw_lines(draw, lines, PAD, PAD - 6)
    if clock:
        draw.text((width - PAD - FONTS.body(26).getlength(clock), PAD + body_h - 8), clock,
                  font=FONTS.body(26), fill=TEXT_TIME if not outgoing else (196, 216, 236))
    buttons: list[tuple[int, int, int, int]] = []
    if rows:
        top = PAD + max(body_h, 8) + (34 if clock else 8) + 6
        grid_w = width - 2 * 10
        draw.rounded_rectangle([10, top, width - 11, height - 1], radius=RADIUS - 6, fill=BUTTON_BG)
        for row_index, cells in enumerate(rows):
            row_y = top + row_index * BUTTON_H
            for cell in cells:
                x0 = 10 + cell["x"]
                cell_w = cell["w"]
                label = strip_icons(cell["label"])
                icon_chars = [ch for ch in str(cell["label"]) if is_icon_char(ch)]
                size = BUTTON_SIZE
                while size > 30:
                    font = FONTS.brand(size)
                    text_w = font.getlength(label)
                    icon_w = len(icon_chars) * (size + 2)
                    if text_w + icon_w + 24 <= cell_w:
                        break
                    size -= 2
                font = FONTS.brand(size)
                text_w = font.getlength(label)
                icon_w = len(icon_chars) * (size + 2)
                total_w = text_w + icon_w + (8 if icon_chars and label else 0)
                start_x = min(x0 + max(10, (cell_w - total_w) // 2), x0 + cell_w - total_w - 10)
                centre_y = row_y + (BUTTON_H - size) // 2 - 2
                if icon_chars:
                    box_size = size + 2
                    draw_icon(draw, icon_chars[0], (start_x, centre_y, start_x + box_size, centre_y + box_size),
                              BUTTON_TEXT)
                    start_x += box_size + 8
                draw.text((start_x, centre_y), label, font=font, fill=BUTTON_TEXT)
                buttons.append((x0, row_y, x0 + cell_w, row_y + BUTTON_H))
            if row_index < len(rows) - 1:
                draw.line([(14, row_y + BUTTON_H), (width - 15, row_y + BUTTON_H)], fill=HAIRLINE, width=2)
        for cells in rows:
            for cell in cells[1:]:
                x = 10 + cell["x"]
                draw.line([(x, top + 6), (x, height - 7)], fill=HAIRLINE, width=2)
    return Layer(layer, height, buttons, width)


def render_media_bubble(kind: str, media: Sequence[str], caption: str,
                        markup: dict[str, Any] | None, clock: str = "",
                        max_w: int = BUBBLE_MAX_W) -> Layer:
    width = max_w
    inner = width - 2 * 10
    layer = Image.new("RGBA", (width, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    blocks: list[Image.Image] = []
    if kind == "album":
        cells = [path for path in media if path]
        columns = 2
        rows = math.ceil(len(cells) / columns)
        gap = 8
        cell_w = (inner - gap * (columns - 1)) // columns
        cell_h = int(cell_w * 0.78)
        grid = Image.new("RGBA", (inner, rows * cell_h + gap * (rows - 1)), (0, 0, 0, 0))
        for index, path in enumerate(cells[:columns * rows]):
            image = load_image(path.split(" — ")[0])
            if not image:
                continue
            tile = cover(image, (cell_w, cell_h))
            grid.paste(tile, ((index % columns) * (cell_w + gap), (index // columns) * (cell_h + gap)))
        grid.putalpha(rounded(grid.size, 20))
        blocks.append(grid)
    elif kind in ("photo", "video"):
        image = load_image(media[0]) if media else None
        if image:
            target_h = int(inner * image.height / image.width)
            target_h = min(max(target_h, 320), 860)
            tile = cover(image, (inner, target_h))
            if kind == "video":
                overlay = Image.new("RGBA", tile.size, (0, 0, 0, 0))
                pen = ImageDraw.Draw(overlay)
                pen.rectangle([0, 0, tile.width, tile.height], fill=(6, 9, 12, 78))
                radius = 74
                cx, cy = tile.width // 2, tile.height // 2
                pen.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(12, 16, 20, 150))
                pen.polygon([(cx - 24, cy - 38), (cx - 24, cy + 38), (cx + 40, cy)], fill=(240, 246, 252, 235))
                tile = Image.alpha_composite(tile.convert("RGBA"), overlay).convert("RGB")
            tile.putalpha(rounded(tile.size, 20))
            blocks.append(tile.convert("RGBA"))
    elif kind == "document":
        block = Image.new("RGBA", (inner, 150), (0, 0, 0, 0))
        pen = ImageDraw.Draw(block)
        pen.rounded_rectangle([0, 12, 120, 138], radius=18, fill=(58, 78, 100, 255))
        draw_icon(pen, 0x1F9FE, (26, 34, 94, 116), (226, 236, 246))
        name = str(media[0]) if media else "файл"
        pen.text((150, 34), name, font=FONTS.body(38), fill=TEXT)
        pen.text((150, 84), "файл бота", font=FONTS.body(28), fill=TEXT_DIM)
        blocks.append(block)
    elif kind == "invoice":
        block = Image.new("RGBA", (inner, 210), (0, 0, 0, 0))
        pen = ImageDraw.Draw(block)
        pen.rounded_rectangle([0, 0, inner - 1, 209], radius=20, fill=(24, 32, 42, 255))
        draw_icon(pen, 0x1F4B3, (28, 34, 104, 110), ACCENT)
        title = str(media[0]) if media else "Счёт"
        pen.text((136, 34), title, font=FONTS.brand(44), fill=TEXT)
        amount = str(media[1]) if len(media) > 1 else ""
        pen.text((136, 96), amount, font=FONTS.body(38, bold=True), fill=ACCENT)
        pen.text((136, 152), "оплата в Telegram", font=FONTS.body(28), fill=TEXT_DIM)
        blocks.append(block)

    caption_lines = wrap_text(caption, width - 2 * PAD) if caption else []
    rows = layout_buttons(button_rows(markup), width - 20) if button_rows(markup) else []
    height = 10 + sum(block.height + 8 for block in blocks)
    height += text_height(caption_lines) + (18 if caption_lines else 0)
    height += (34 if clock else 0) + 14
    if rows:
        height += len(rows) * BUTTON_H + 12

    layer = Image.new("RGBA", (width, max(height, 80)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([0, 0, width - 1, max(height, 80) - 1], radius=RADIUS, fill=BUBBLE_IN)
    cursor = 10
    for block in blocks:
        layer.alpha_composite(block, (10, cursor))
        cursor += block.height + 8
    if caption_lines:
        draw_lines(draw, caption_lines, PAD, cursor + 4)
        cursor += text_height(caption_lines) + 10
    if clock:
        draw.text((width - PAD - FONTS.body(26).getlength(clock), cursor - 4), clock,
                  font=FONTS.body(26), fill=TEXT_TIME)
        cursor += 30
    buttons: list[tuple[int, int, int, int]] = []
    if rows:
        top = cursor + 4
        grid_w = width - 20
        draw.rounded_rectangle([10, top, width - 11, height - 1], radius=RADIUS - 6, fill=BUTTON_BG)
        for row_index, cells in enumerate(rows):
            row_y = top + row_index * BUTTON_H
            for cell in cells:
                x0 = 10 + cell["x"]
                font = FONTS.brand(BUTTON_SIZE)
                label = strip_icons(cell["label"])
                icon_chars = [ch for ch in str(cell["label"]) if is_icon_char(ch)]
                text_w = font.getlength(label)
                icon_w = len(icon_chars) * (BUTTON_SIZE + 2)
                start_x = x0 + max(12, (cell["w"] - text_w - icon_w - (8 if icon_chars and label else 0)) // 2)
                centre_y = row_y + (BUTTON_H - BUTTON_SIZE) // 2 - 2
                if icon_chars:
                    box_size = BUTTON_SIZE + 2
                    draw_icon(draw, icon_chars[0], (start_x, centre_y, start_x + box_size, centre_y + box_size),
                              BUTTON_TEXT)
                    start_x += box_size + 8
                draw.text((start_x, centre_y), label, font=font, fill=BUTTON_TEXT)
                buttons.append((x0, row_y, x0 + cell["w"], row_y + BUTTON_H))
            if row_index < len(rows) - 1:
                draw.line([(14, row_y + BUTTON_H), (width - 15, row_y + BUTTON_H)], fill=HAIRLINE, width=2)
        for cells in rows:
            for cell in cells[1:]:
                x = 10 + cell["x"]
                draw.line([(x, top + 6), (x, height - 7)], fill=HAIRLINE, width=2)
    return Layer(layer, max(height, 80), buttons, width)


def render_contact(name: str, phone: str, clock: str = "") -> Layer:
    width = 620
    height = 176
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=RADIUS, fill=BUBBLE_OUT)
    draw.ellipse([PAD, 34, PAD + 104, 138], fill=(226, 238, 250, 255))
    draw_icon(draw, 0x1F464, (PAD + 22, 52, PAD + 86, 120), (52, 88, 124))
    draw.text((PAD + 132, 40), name, font=FONTS.body(40, bold=True), fill=TEXT)
    draw.text((PAD + 132, 94), phone, font=FONTS.body(38), fill=(198, 220, 240))
    if clock:
        draw.text((width - PAD - FONTS.body(26).getlength(clock), height - 46), clock,
                  font=FONTS.body(26), fill=(196, 216, 236))
    return Layer(layer, height, (), width)


# ------------------------------------------------------------------- сцены

class Message:
    """Сообщение в кадре: появление и все последующие правки на месте."""

    def __init__(self, key: int, outgoing: bool, appear: float, clock: str = ""):
        self.key = key
        self.outgoing = outgoing
        self.appear = appear
        self.clock = clock
        self.versions: list[tuple[float, Layer]] = []

    def add(self, at: float, layer: Layer) -> None:
        self.versions.append((at, layer))

    def state(self, t: float) -> tuple[Layer | None, Layer | None, float]:
        """Текущий слой, предыдущий и прогресс перехода 0..1."""
        current = None
        previous = None
        progress = 1.0
        for index, (at, layer) in enumerate(self.versions):
            if at <= t:
                previous = current
                current = layer
                if previous is not None:
                    progress = min(1.0, (t - at) / MORPH)
        return current, previous, progress

    @property
    def first(self) -> Layer | None:
        return self.versions[0][1] if self.versions else None


MORPH = 0.30
APPEAR = 0.26


def ease_out(value: float) -> float:
    return 1 - (1 - min(max(value, 0.0), 1.0)) ** 3


def lerp(a: float, b: float, p: float) -> float:
    return a + (b - a) * p


class Scene:
    """Один чат во времени. ``frame(t)`` вызывается по возрастанию ``t``."""

    def __init__(self, title: str, status: str, avatar: str | None,
                 messages: Sequence[Message], typings: Sequence[tuple[float, float]] = (),
                 taps: Sequence[tuple[float, float, int, int]] = (),
                 keyboards: Sequence[tuple[float, float | None, list[list[dict[str, Any]]]]] = (),
                 duration: float = 0.0):
        self.title = title
        self.status = status
        self.avatar = avatar
        self.messages = list(messages)
        self.typings = list(typings)
        self.taps = list(taps)
        self.keyboards = list(keyboards)
        self.duration = duration or (max([m.appear for m in self.messages] or [0.0]) + 3.0)
        self._scroll = 0.0
        self._heights: dict[int, float] = {}
        self._bg = self._background()
        self._header = self._header()

    # ---------------------------------------------------------------- заготовки

    def _background(self) -> Image.Image:
        bg = Image.new("RGB", (W, H), BG_BOTTOM)
        draw = ImageDraw.Draw(bg)
        steps = 96
        for index in range(steps):
            p = index / (steps - 1)
            color = tuple(int(lerp(BG_TOP[i], BG_BOTTOM[i], p)) for i in range(3))
            draw.rectangle([0, int(p * H), W, int(p * H) + H // steps + 2], fill=color)
        # Едва заметная диагональная штриховка: кадр не выглядит мёртвым градиентом.
        texture = Image.new("L", (W, H), 0)
        pen = ImageDraw.Draw(texture)
        for offset in range(-H, W + H, 168):
            pen.line([(offset, 0), (offset + H, H)], fill=9, width=44)
        texture = texture.filter(ImageFilter.GaussianBlur(26))
        bg = Image.composite(Image.new("RGB", (W, H), (255, 255, 255)), bg, texture.point(lambda v: v // 3))
        vignette = Image.new("L", (W, H), 0)
        pen = ImageDraw.Draw(vignette)
        pen.rectangle([0, 0, W, H], fill=44)
        pen.ellipse([-260, -520, W + 260, H + 520], fill=0)
        vignette = vignette.filter(ImageFilter.GaussianBlur(180))
        return Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), bg, vignette)

    def _header(self) -> Image.Image:
        layer = Image.new("RGBA", (W, HEADER_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rectangle([0, 0, W, HEADER_H - 2], fill=HEADER_BG + (246,))
        draw.line([(0, HEADER_H - 2), (W, HEADER_H - 2)], fill=HAIRLINE, width=2)
        # «Назад»
        draw.line([(52, 62), (34, 80), (52, 98)], fill=TEXT, width=6, joint="curve")
        # Аватар бота
        size = 96
        x0, y0 = 92, (HEADER_H - size) // 2 - 4
        avatar = load_image(self.avatar) if self.avatar else None
        if avatar:
            tile = cover(avatar, (size, size))
        else:
            tile = Image.new("RGB", (size, size), (46, 62, 80))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        layer.paste(tile, (x0, y0), mask)
        draw.ellipse([x0, y0, x0 + size - 1, y0 + size - 1], outline=(255, 255, 255, 40), width=2)
        draw.text((x0 + size + 26, y0 + 6), self.title, font=FONTS.brand(48), fill=TEXT)
        draw.text((x0 + size + 26, y0 + 60), self.status, font=FONTS.body(30), fill=TEXT_DIM)
        return layer

    # ------------------------------------------------------------------ раскладка

    def _keyboard(self, t: float) -> list[list[dict[str, Any]]]:
        rows: list[list[dict[str, Any]]] = []
        for start, end, candidate in self.keyboards:
            if t >= start and (end is None or t < end):
                rows = candidate
        return rows

    def _heights_at(self, t: float) -> list[tuple[Message, Layer, Layer | None, float, float]]:
        """Сообщения, видимые в момент t, с их текущей высотой."""
        items = []
        for message in self.messages:
            if message.appear > t:
                continue
            current, previous, progress = message.state(t)
            if current is None:
                continue
            if previous is not None and progress < 1.0:
                height = lerp(previous.height, current.height, ease_out(progress))
            else:
                height = float(current.height)
            items.append((message, current, previous, progress, height))
        return items

    def frame(self, t: float) -> Image.Image:
        rows = self._keyboard(t)
        keyboard_h = 0
        if rows:
            keyboard_h = 34 + len(rows) * 104 + 22
        view_top = HEADER_H + 12
        view_bottom = H - (keyboard_h + 10 if keyboard_h else 26)
        viewport = view_bottom - view_top

        items = self._heights_at(t)
        typing_now = any(start <= t < end for start, end in self.typings)
        typing_h = 108 if typing_now else 0
        total = sum(item[4] + GAP for item in items) + typing_h
        target = max(0.0, total - viewport)
        # Плавное следование за новыми сообщениями.
        self._scroll += (target - self._scroll) * 0.22
        if abs(target - self._scroll) < 1.5:
            self._scroll = target

        canvas = self._bg.copy()
        area = Image.new("RGBA", (W, viewport), (0, 0, 0, 0))
        pen = ImageDraw.Draw(area)
        y = viewport - total + int(target - self._scroll)

        # Дата-чип, как в живом чате: уезжает вверх вместе с перепиской.
        chip_w, chip_h = 250, 62
        if 4 <= y < viewport:
            chip_x = (W - chip_w) // 2
            pen.rounded_rectangle([chip_x, y, chip_x + chip_w, y + chip_h],
                                  radius=chip_h // 2, fill=(255, 255, 255, 26))
            label = "Сегодня"
            font = FONTS.body(30)
            pen.text((chip_x + (chip_w - font.getlength(label)) // 2, y + 13), label,
                     font=font, fill=TEXT_DIM)
        y += chip_h + 26

        for message, current, previous, progress, height in items:
            age = t - message.appear
            alpha = ease_out(age / APPEAR)
            slide = int(lerp(26, 0, alpha))
            x = W - MARGIN_X - current.width if message.outgoing else MARGIN_X
            box_y = int(y + (height - current.height) + slide)
            if box_y + current.height > -40 and box_y < viewport + 40:
                if previous is not None and progress < 1.0:
                    fade = ease_out(progress)
                    ghost = Image.new("RGBA", (current.width, current.height), (0, 0, 0, 0))
                    old = previous.image.copy()
                    old.putalpha(old.getchannel("A").point(lambda v: int(v * (1 - fade))))
                    new = current.image.copy()
                    new.putalpha(new.getchannel("A").point(lambda v: int(v * fade)))
                    if message.outgoing:
                        ghost.alpha_composite(old, (0, max(0, previous.height - current.height)))
                    else:
                        ghost.alpha_composite(old, (0, max(0, previous.height - current.height)))
                    ghost.alpha_composite(new, (0, int(lerp(10, 0, fade))))
                    area.alpha_composite(ghost, (x, box_y))
                else:
                    piece = current.image.copy()
                    if alpha < 1.0:
                        piece.putalpha(piece.getchannel("A").point(lambda v: int(v * alpha)))
                    area.alpha_composite(piece, (x, box_y))
                # Отклик на нажатие: подсветка той кнопки, которую нажали.
                for start, end, key, index in self.taps:
                    if key == message.key and start <= t < end and index < len(current.buttons):
                        pulse = 1.0 - (t - start) / max(1e-6, end - start)
                        x0, y0, x1, y1 = current.buttons[index]
                        overlay = Image.new("RGBA", (current.width, current.height), (0, 0, 0, 0))
                        op = ImageDraw.Draw(overlay)
                        op.rounded_rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], radius=20,
                                             fill=(255, 255, 255, int(70 * pulse)))
                        glow = overlay.filter(ImageFilter.GaussianBlur(10))
                        area.alpha_composite(glow, (x, box_y))
            y += height + GAP

        if typing_now:
            bubble = Image.new("RGBA", (190, 104), (0, 0, 0, 0))
            pen2 = ImageDraw.Draw(bubble)
            pen2.rounded_rectangle([0, 0, 189, 103], radius=RADIUS, fill=BUBBLE_IN)
            for index in range(3):
                phase = (t * 3.4 + index * 0.42) % 1.0
                lift = math.sin(phase * math.pi) * 9
                radius = 11
                cx = 48 + index * 46
                cy = 54 - lift
                shade = int(lerp(120, 226, math.sin(phase * math.pi)))
                pen2.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                             fill=(shade, shade + 8, shade + 16, 255))
            area.alpha_composite(bubble, (MARGIN_X, int(y)))

        canvas.paste(area, (0, view_top), area)
        canvas.paste(self._header.convert("RGB"), (0, 0), self._header)

        if keyboard_h:
            panel = Image.new("RGBA", (W, keyboard_h + 8), (0, 0, 0, 0))
            pen3 = ImageDraw.Draw(panel)
            pen3.rectangle([0, 8, W, keyboard_h + 8], fill=KEYBOARD_BG + (250,))
            pen3.line([(0, 8), (W, 8)], fill=HAIRLINE, width=2)
            key_y = 34
            for row in rows:
                if not row:
                    continue
                total_w = W - 2 * 16
                weights = [max(FONTS.body(34).getlength(strip_icons(str(key.get("text", "")))) + 40, 120)
                           for key in row]
                scale = total_w / sum(weights)
                key_x = 16
                for key, weight in zip(row, weights):
                    key_w = int(weight * scale) - 10
                    pen3.rounded_rectangle([key_x, key_y, key_x + key_w, key_y + 92], radius=18,
                                           fill=KEY_BG)
                    label = strip_icons(str(key.get("text", "")))
                    font = FONTS.body(34)
                    icons = [ch for ch in str(key.get("text", "")) if is_icon_char(ch)]
                    text_w = font.getlength(label)
                    icon_w = len(icons) * 40
                    start_x = key_x + max(14, (key_w - text_w - icon_w) // 2)
                    if icons:
                        draw_icon(pen3, icons[0], (start_x, key_y + 26, start_x + 40, key_y + 66), TEXT)
                        start_x += 48
                    pen3.text((start_x, key_y + 26), label, font=font, fill=TEXT)
                    key_x += key_w + 10
                key_y += 104
            canvas.paste(panel, (0, H - keyboard_h - 8), panel)

        # Мягкое появление и уход кадра.
        fade = min(t / 0.5, (self.duration - t) / 0.7, 1.0)
        if fade < 1.0:
            shade = Image.new("RGB", (W, H), (0, 0, 0))
            canvas = Image.composite(canvas, shade, Image.new("L", (W, H), int(255 * max(fade, 0.0))))
        return canvas


def title_frame(word: str, progress: float) -> Image.Image:
    """Межкадровая заставка роли: одно слово на чёрном, без пояснений."""
    base = Image.new("RGB", (W, H), (6, 8, 11))
    pen = ImageDraw.Draw(base)
    for offset in range(-H, W + H, 210):
        pen.line([(offset, 0), (offset + H, H)], fill=(12, 15, 19), width=54)
    fade = math.sin(min(max(progress, 0.0), 1.0) * math.pi)
    size = 150
    while size > 60:
        font = FONTS.display(size)
        if font.getlength(word) <= W * 0.76:
            break
        size -= 6
    font = FONTS.display(size)
    width = font.getlength(word)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pen2 = ImageDraw.Draw(layer)
    pen2.text(((W - width) // 2, H // 2 - 150), word, font=font,
              fill=(232, 240, 248, int(255 * fade)))
    pen2.line([(W // 2 - 190, H // 2 + 90), (W // 2 + 190, H // 2 + 90)],
              fill=(146, 197, 240, int(200 * fade)), width=4)
    base.paste(layer, (0, 0), layer)
    return base


def sample_frame(path: str | Path) -> None:
    """Один кадр со всеми типами сообщений — проверить вид до съёмки видео."""
    messages: list[Message] = []
    clock = "20:41"

    def add(outgoing: bool, layer: Layer, at: float) -> Message:
        message = Message(len(messages), outgoing, at, clock)
        message.add(at, layer)
        messages.append(message)
        return message

    t = 0.2
    add(False, render_media_bubble("photo", ["assets/drop/hero-sila-chest-v1.jpg"],
                                   "<b>ВОРОЖБИТОВ</b>\nНикита, ты на закрытой территории.",
                                   None, clock), t)
    t += 0.2
    add(False, render_text_bubble("<b>ВИТРИНА</b> · 2 вещи\n\nРазделы и остатки честные: что видно, то и есть.",
                                  {"inline_keyboard": [[{"text": "Выпуск · 1 вещь", "callback_data": "cat:drop"}],
                                                       [{"text": "📐 Подобрать размер", "callback_data": "sg"},
                                                        {"text": "← Главное меню", "callback_data": "menu"}]]},
                                  False, clock), t)
    t += 0.2
    add(False, render_media_bubble("album", ["assets/spin/tee-sich-01-v2.jpg", "assets/spin/tee-sich-05-v2.jpg",
                                             "assets/drop/tee-flatlay-v2.jpg", "assets/drop/tee-detail-v4.jpg"],
                                   "", None, clock), t)
    t += 0.2
    add(True, render_text_bubble("+7 999 123-45-67", None, True, clock), t)
    t += 0.2
    add(False, render_media_bubble("invoice", ["СИЛА И ЧЕСТЬ · L", "4 900 ₽"], "",
                                   {"inline_keyboard": [[{"text": "⭐ Оплатить звёздами", "callback_data": "pay"}]]},
                                   clock), t)
    t += 0.2
    add(False, render_text_bubble("<b>ПОКУПКА ПРИНЯТА · 4 900 ₽</b>\n\n<code>СИЛА И ЧЕСТЬ · L</code>\n"
                                  "🚚 Доставка: согласуем в переписке",
                                  {"inline_keyboard": [[{"text": "📦 Мои покупки", "callback_data": "my_orders"}]]},
                                  False, clock), t)

    scene = Scene("ВОРОЖБИТОВ", "бот", "assets/bot-avatar.jpg", messages,
                  typings=[(100.0, 101.0)], taps=[(100.0, 101.0, 1, 0)],
                  keyboards=[(0.0, None, [[{"text": "📱 Отправить номер телефона", "request_contact": True}],
                                          [{"text": "Скрыть клавиатуру"}]])],
                  duration=4.0)
    scene._scroll = 0.0
    frame = scene.frame(3.0)
    frame.save(str(path))
    print("кадр сохранён:", path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, help="куда сохранить проверочный кадр")
    args = parser.parse_args()
    sample_frame(args.sample)
