#!/usr/bin/env python3
"""
СИЛА И ЧЕСТЬ — тизер, вариант B «ПЛЁНКА».

Совсем другой язык, чем у варианта A (Mini App / Inter / красный тикер):
  • кадр 3:4 в чёрных полях — как слайд в рамке; сверху мелкий бёрн-ин, снизу титр;
  • титры набраны уставом (Ruslan Display) — тем же строем букв, что и принт на груди;
  • плёночный грейд: bleach bypass, тёплые света, холодные тени, приподнятый чёрный,
    крупное зерно, дрожание кадра, мерцание экспозиции, царапины, пыль, засветки;
  • монтаж: жёсткие склейки со вспышкой, срыв кадра в проекторе, стробо-нарезка ч/б,
    затем длинный герой-кадр и финальный меч с монограммой ВВ;
  • звук: боевой барабан, низкий хор-пад, ветер, стрёкот проектора, наковальня и
    «шинг» меча на ключевых склейках — никакого дрона/дождя из варианта A.

Запуск: python3 video/make_video_v2_film.py   (~3–4 мин на 2 ядрах)
"""
from __future__ import annotations

import math
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy.signal import butter, sosfilt

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover
    FFMPEG = "ffmpeg"

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "video"
USTAV = OUT_DIR / "fonts" / "RuslanDisplay.ttf"
CAPTION = OUT_DIR / "fonts" / "Oswald-Regular.ttf"
OUT_FILE = OUT_DIR / "sila-i-chest-teaser-v2-film.mp4"

W, H = 1080, 1920
FPS = 30
# окно кадра 3:4
IW, IH = 1080, 1440
IY = 232                    # верхнее поле 232 px, нижнее 248 px
BAR_BOTTOM_CY = IY + IH + 124

BLACK = (6, 5, 5)
CREAM = (236, 228, 212)
DIM = (128, 120, 108)
BLOOD = (168, 34, 30)

# ------------------------------------------------------------------ сценарий
# (файл, титр, сек, фокус (x, y), движение)
SHOTS = [
    ("sila-i-chest-07-geliks.jpg",      "НОЧЬ",     3.2, (0.50, 0.42), "push"),
    ("sila-i-chest-02-crew.jpg",        "СВОИ",     2.8, (0.50, 0.45), "still"),
    ("sila-i-chest-05-roof.jpg",        "ГОРОД",    2.8, (0.50, 0.45), "still"),
    ("sila-i-chest-03-gym.jpg",         "ЖЕЛЕЗО",   2.2, (0.50, 0.42), "punch"),
    ("sila-i-chest-04-boxing-back.jpg", "ТЯГА",     1.8, (0.50, 0.40), "punch"),
    ("sila-i-chest-10-forge.jpg",       "ХАРАКТЕР", 1.6, (0.52, 0.42), "punch"),
    ("sila-i-chest-04-boxing-back.jpg", "МЕЧ",      1.4, (0.50, 0.40), "still"),
    ("sila-i-chest-07-geliks.jpg",      "СВОЙ ПУТЬ",1.2, (0.50, 0.45), "still"),
]
SLIP_SRC = "sila-i-chest-05-roof.jpg"
MONTAGE = [  # стробо-нарезка ч/б, 0.25 с каждый
    ("sila-i-chest-02-crew.jpg", (0.50, 0.42)),
    ("sila-i-chest-09-ring.jpg", (0.45, 0.35)),
    ("sila-i-chest-03-gym.jpg", (0.50, 0.38)),
    ("sila-i-chest-10-forge.jpg", (0.52, 0.38)),
    ("sila-i-chest-08-dog.jpg", (0.66, 0.40)),
    ("sila-i-chest-04-boxing-back.jpg", (0.50, 0.38)),
]
HERO = ("sila-i-chest-11-geliks-vertical.jpg", "СИЛА И ЧЕСТЬ", 3.6, (0.50, 0.42), "push")
FLATLAY = "sila-i-chest-06-flatlay.jpg"

INTRO_SEC, SLIP_FRAMES, MONTAGE_SEC, FLAT_SEC, OUTRO_SEC = 2.6, 5, 0.25, 2.6, 3.2


# --------------------------------------------------------------- типографика
@lru_cache(maxsize=None)
def ustav(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(USTAV), size)


@lru_cache(maxsize=None)
def caption(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(CAPTION), size)


def tracked_w(text: str, f: ImageFont.FreeTypeFont, tr: float) -> float:
    return f.getlength(text) + tr * f.size * max(0, len(text) - 1)


def draw_tracked(d: ImageDraw.ImageDraw, x: float, y: float, text: str, f: ImageFont.FreeTypeFont,
                 fill, tr: float = 0.0, align: str = "left") -> None:
    total = tracked_w(text, f, tr)
    if align == "center":
        x -= total / 2
    elif align == "right":
        x -= total
    for i, ch in enumerate(text):
        d.text((x, y), ch, font=f, fill=fill, anchor="ls")
        x += f.getlength(ch) + tr * f.size


def fit_ustav(text: str, max_size: int, max_w: int) -> int:
    size = max_size
    while size > 60 and ustav(size).getlength(text) > max_w:
        size -= 4
    return size


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def rgba(c, a: float):
    return (*c, int(255 * min(1.0, max(0.0, a))))


# ------------------------------------------------------------ плёночные слои
_vig: Image.Image | None = None
_gate: Image.Image | None = None
_leak: np.ndarray | None = None


def vignette() -> Image.Image:
    global _vig
    if _vig is None:
        yy, xx = np.mgrid[0:IH, 0:IW]
        r = np.sqrt(((xx - IW / 2) / (IW / 2)) ** 2 + ((yy - IH / 2) / (IH / 2)) ** 2)
        _vig = Image.fromarray((np.clip((r - 0.45) / 0.8, 0, 1) ** 1.5 * 0.9 * 255).astype(np.uint8), "L")
    return _vig


def gate_mask() -> Image.Image:
    """Скруглённые углы окна, как у слайда."""
    global _gate
    if _gate is None:
        m = Image.new("L", (IW, IH), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, IW - 1, IH - 1), radius=22, fill=255)
        _gate = m
    return _gate


def light_leak() -> np.ndarray:
    global _leak
    if _leak is None:
        yy, xx = np.mgrid[0:IH, 0:IW].astype(np.float32)
        d = np.sqrt(((xx - IW * 1.05) / (IW * 0.55)) ** 2 + ((yy - IH * 0.35) / (IH * 0.6)) ** 2)
        a = np.clip(1 - d, 0, 1) ** 1.6
        col = np.array([1.0, 0.45, 0.12], dtype=np.float32)
        _leak = a[..., None] * col[None, None, :]
    return _leak


def film_grade(win: Image.Image, rng: np.random.Generator, flicker: float, bw: bool = False,
               leak: float = 0.0) -> Image.Image:
    arr = np.asarray(win, dtype=np.float32) / 255.0
    lum = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if bw:
        arr = np.repeat(np.clip((lum - 0.5) * 1.7 + 0.5, 0, 1)[..., None], 3, axis=2)
    else:
        arr = lum[..., None] + (arr - lum[..., None]) * 0.38
        arr = (arr - 0.45) * 1.22 + 0.45
        l2 = (lum ** 2)[..., None]
        s2 = ((1 - lum) ** 2)[..., None]
        arr += l2 * np.array([0.05, 0.02, -0.03], dtype=np.float32)
        arr += s2 * np.array([-0.01, 0.0, 0.035], dtype=np.float32)
    arr = arr * 0.95 + 0.05          # приподнятый чёрный
    arr *= flicker
    if leak > 0:
        lk = light_leak() * leak
        arr = 1 - (1 - np.clip(arr, 0, 1)) * (1 - lk)   # screen
    grain = rng.normal(0, 0.034 if bw else 0.026, size=(IH // 2, IW // 2, 1)).astype(np.float32)
    arr += np.repeat(np.repeat(grain, 2, axis=0), 2, axis=1)
    np.clip(arr, 0, 1, out=arr)
    out = Image.fromarray((arr * 255).astype(np.uint8), "RGB")
    out = Image.composite(Image.new("RGB", (IW, IH), BLACK), out, vignette())
    return out


def cover(img: Image.Image, zoom: float, cx: float, cy: float, dx: float = 0, dy: float = 0) -> Image.Image:
    iw, ih = img.size
    scale = max(IW / iw, IH / ih) * zoom
    cw, ch = IW / scale, IH / scale
    x0 = min(max(cx * iw - cw / 2 + dx / scale, 0), iw - cw)
    y0 = min(max(cy * ih - ch / 2 + dy / scale, 0), ih - ch)
    return img.resize((IW, IH), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))


class Weave:
    """Дрожание кадра + мерцание экспозиции: плавные случайные блуждания."""

    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.dx = self.dy = 0.0
        self.fl = 1.0

    def step(self) -> tuple[float, float, float]:
        self.dx = self.dx * 0.85 + self.rng.normal(0, 1.1)
        self.dy = self.dy * 0.85 + self.rng.normal(0, 1.4)
        self.fl = self.fl * 0.7 + (1 + self.rng.normal(0, 0.028)) * 0.3
        return max(-6, min(6, self.dx)), max(-7, min(7, self.dy)), max(0.9, min(1.08, self.fl))


def dust_and_scratches(win: Image.Image, rng: np.random.Generator, scratches: list[tuple[float, float]]) -> Image.Image:
    layer = Image.new("RGBA", (IW, IH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x, a in scratches:
        d.line((x, 0, x + rng.normal(0, 2), IH), fill=rgba(CREAM, a), width=1)
    for _ in range(rng.integers(0, 4)):
        x, y = rng.uniform(0, IW), rng.uniform(0, IH)
        r = rng.uniform(1.2, 3.5)
        col = CREAM if rng.random() < 0.5 else BLACK
        d.ellipse((x - r, y - r, x + r, y + r), fill=rgba(col, rng.uniform(0.3, 0.7)))
    if rng.random() < 0.06:  # волосок
        x, y = rng.uniform(0, IW), rng.uniform(0, IH)
        pts = [(x + rng.normal(0, 40) * k, y + 30 * k + rng.normal(0, 8)) for k in range(4)]
        d.line(pts, fill=rgba(BLACK, 0.7), width=2, joint="curve")
    return Image.alpha_composite(win.convert("RGBA"), layer).convert("RGB")


class ScratchPlan:
    def __init__(self, rng: np.random.Generator, n_frames: int, density: float = 0.35):
        self.events: list[tuple[int, int, float, float]] = []
        for _ in range(int(max(1, n_frames / FPS * density * 3))):
            s = int(rng.uniform(0, n_frames))
            self.events.append((s, s + int(rng.uniform(2, 9)), rng.uniform(20, IW - 20), rng.uniform(0.08, 0.22)))

    def at(self, i: int) -> list[tuple[float, float]]:
        return [(x, a) for s, e, x, a in self.events if s <= i < e]


# -------------------------------------------------------------- сборка кадра
def compose(win: Image.Image, top_left: str = "", top_right: str = "", title: str = "",
            title_a: float = 1.0, title_scale: float = 1.0, title_max: int = 164) -> Image.Image:
    frame = Image.new("RGB", (W, H), BLACK)
    frame.paste(win, (0, IY), gate_mask())
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # тонкая рамка слайда
    d.rounded_rectangle((0, IY, IW - 1, IY + IH - 1), radius=22, outline=rgba(CREAM, 0.22), width=2)
    # верхнее поле: бёрн-ин + орнамент
    f = caption(24)
    if top_left:
        draw_tracked(d, 48, 130, top_left, f, rgba(DIM, 0.9), tr=0.22)
    if top_right:
        draw_tracked(d, W - 48, 130, top_right, f, rgba(DIM, 0.9), tr=0.22, align="right")
    cx, cy = W / 2, 122
    d.line((cx - 150, cy, cx - 14, cy), fill=rgba(DIM, 0.6), width=1)
    d.line((cx + 14, cy, cx + 150, cy), fill=rgba(DIM, 0.6), width=1)
    d.polygon([(cx, cy - 7), (cx + 7, cy), (cx, cy + 7), (cx - 7, cy)], outline=rgba(CREAM, 0.7))
    frame = Image.alpha_composite(frame.convert("RGBA"), layer)
    if title and title_a > 0:
        size = fit_ustav(title, title_max, W - 120)
        size_s = max(8, int(size * title_scale))
        tf = ustav(size_s)
        tw = tf.getlength(title)
        tl = Image.new("RGBA", (W, 300), (0, 0, 0, 0))
        td = ImageDraw.Draw(tl)
        # мягкая тень/свечение под буквами
        td.text(((W - tw) / 2 + 3, 150 + 4), title, font=tf, fill=rgba((0, 0, 0), 0.8 * title_a), anchor="ls")
        td.text(((W - tw) / 2, 150), title, font=tf, fill=rgba(CREAM, title_a), anchor="ls")
        frame.alpha_composite(tl, (0, int(BAR_BOTTOM_CY + size * 0.36 - 150)))
    return frame.convert("RGB")


def title_anim(t: float, t0: float, fast: bool = False) -> tuple[float, float]:
    """Штамп: масштаб 1.22→1 и альфа 0→1 за 5 кадров."""
    dur = 0.12 if fast else 0.17
    p = ease((t - t0) / dur)
    if t < t0:
        return 0.0, 1.22
    return p, 1.22 - 0.22 * p


# ------------------------------------------------------------------ сегменты
def seg_intro(rng: np.random.Generator):
    n = int(INTRO_SEC * FPS)
    weave = Weave(rng)
    for i in range(n):
        t = i / FPS
        dx, dy, fl = weave.step()
        base = np.full((IH, IW, 3), 0.03, dtype=np.float32)
        # ракорд: первые 0.5 с — мигание проектора и засветка
        if t < 0.5:
            base += rng.uniform(0.0, 0.12) * (1 if rng.random() < 0.6 else 0)
            leak = max(0.0, 0.9 - t * 1.6) * rng.uniform(0.5, 1.0)
        else:
            leak = 0.0
        win = film_grade(Image.fromarray((np.clip(base, 0, 1) * 255).astype(np.uint8), "RGB"), rng, fl, leak=leak)
        frame = Image.new("RGB", (W, H), BLACK)
        frame.paste(win, (0, IY), gate_mask())
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, IY, IW - 1, IY + IH - 1), radius=22, outline=rgba(CREAM, 0.22), width=2)
        # ВОРОЖБИТОВ — штамп на 0.55 с
        a, s = title_anim(t, 0.55)
        if a > 0:
            size = int(fit_ustav("ВОРОЖБИТОВ", 132, W - 140) * s)
            tf = ustav(size)
            tw = tf.getlength("ВОРОЖБИТОВ")
            d.text(((W - tw) / 2 + dx + 3, 940 + dy + 4), "ВОРОЖБИТОВ", font=tf, fill=rgba((0, 0, 0), 0.8 * a), anchor="ls")
            d.text(((W - tw) / 2 + dx, 940 + dy), "ВОРОЖБИТОВ", font=tf, fill=rgba(CREAM, a * fl), anchor="ls")
        a2 = ease((t - 1.05) / 0.35)
        if a2 > 0:
            draw_tracked(d, W / 2 + dx, 1030 + dy, "ПРЕДСТАВЛЯЕТ", caption(30), rgba(DIM, a2), tr=0.42, align="center")
            cx, cy = W / 2, 1100
            d.line((cx - 120, cy, cx - 14, cy), fill=rgba(DIM, 0.6 * a2), width=1)
            d.line((cx + 14, cy, cx + 120, cy), fill=rgba(DIM, 0.6 * a2), width=1)
            d.polygon([(cx, cy - 7), (cx + 7, cy), (cx, cy + 7), (cx - 7, cy)], outline=rgba(CREAM, 0.7 * a2))
        a3 = ease((t - 1.5) / 0.35)
        if a3 > 0:
            draw_tracked(d, W / 2 + dx, 1230 + dy, "ДРОП 001 · ФУТБОЛКА", caption(26), rgba(DIM, 0.8 * a3), tr=0.3, align="center")
        yield Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")


def seg_shot(spec, rng: np.random.Generator, idx: int, total: int):
    file, title, sec, focus, kind = spec
    img = Image.open(ROOT / file).convert("RGB")
    n = int(sec * FPS)
    weave = Weave(rng)
    scratches = ScratchPlan(rng, n)
    fast = sec < 2.0
    for i in range(n):
        t = i / FPS
        p = i / max(1, n - 1)
        dx, dy, fl = weave.step()
        if kind == "push":
            zoom = 1.03 + 0.07 * ease(p)
        elif kind == "punch":  # ступенчатый наезд в такт
            zoom = 1.03 + 0.05 * min(2, int(t / 0.45))
        else:
            zoom = 1.03
        win = cover(img, zoom, *focus, dx, dy)
        leak = 0.0
        if i < 8 and idx in (1, 3):  # засветка на некоторых склейках
            leak = (1 - i / 8) ** 1.5 * 0.8
        win = film_grade(win, rng, fl, leak=leak)
        win = dust_and_scratches(win, rng, scratches.at(i))
        a, s = title_anim(t, 0.12 if fast else 0.28, fast)
        yield compose(win, "ВОРОЖБИТОВ · ДРОП 001", f"КАДР {idx:02d} / {total:02d}", title, a, s)


def seg_slip(rng: np.random.Generator):
    """Срыв кадра в проекторе: изображение уезжает вниз, между кадрами чёрная межкадровая полоса."""
    img = Image.open(ROOT / SLIP_SRC).convert("RGB")
    base = np.asarray(film_grade(cover(img, 1.03, 0.5, 0.42), rng, 1.15), dtype=np.uint8)
    offs = [140, 420, 820, 1240, 1400]
    for k in range(SLIP_FRAMES):
        off = offs[k] % IH
        rolled = np.roll(base, off, axis=0)
        rolled[max(0, off - 28):off + 28, :, :] = 6   # межкадровая полоса
        win = Image.fromarray(rolled, "RGB")
        arr = np.asarray(win, dtype=np.float32) * rng.uniform(0.6, 1.0)
        win = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        yield compose(win, "ВОРОЖБИТОВ · ДРОП 001", "", "")


def seg_montage(rng: np.random.Generator):
    """Стробо-нарезка ч/б: каждый кадр 0.25 с, между ними 1 чёрный кадр."""
    per = int(MONTAGE_SEC * FPS)
    for k, (file, focus) in enumerate(MONTAGE):
        img = Image.open(ROOT / file).convert("RGB")
        for i in range(per):
            if i == 0:
                yield Image.new("RGB", (W, H), BLACK)
                continue
            zoom = 1.18 - 0.10 * (i / per)
            win = film_grade(cover(img, zoom, *focus), rng, 1.0, bw=True)
            if i == 1:  # вспышка
                arr = np.asarray(win, dtype=np.float32)
                win = Image.fromarray(np.clip(arr * 0.55 + 255 * 0.45, 0, 255).astype(np.uint8), "RGB")
            yield compose(win, "", "", "")


def seg_hero(rng: np.random.Generator):
    file, title, sec, focus, _ = HERO
    img = Image.open(ROOT / file).convert("RGB")
    n = int(sec * FPS)
    weave = Weave(rng)
    scratches = ScratchPlan(rng, n, density=0.2)
    for i in range(n):
        t = i / FPS
        p = i / max(1, n - 1)
        dx, dy, fl = weave.step()
        zoom = 1.02 + 0.10 * ease(p)
        win = film_grade(cover(img, zoom, *focus, dx * 0.5, dy * 0.5), rng, fl, leak=(max(0.0, 1 - i / 10) ** 1.5) * 0.9)
        win = dust_and_scratches(win, rng, scratches.at(i))
        a, s = title_anim(t, 0.5)
        yield compose(win, "ВОРОЖБИТОВ · ДРОП 001", "ФУТБОЛКА · ЧЁРНАЯ", title, a, s, title_max=128)


def seg_flatlay(rng: np.random.Generator):
    img = Image.open(ROOT / FLATLAY).convert("RGB")
    n = int(FLAT_SEC * FPS)
    weave = Weave(rng)
    for i in range(n):
        t = i / FPS
        dx, dy, fl = weave.step()
        win = film_grade(cover(img, 1.0 + 0.04 * ease(i / n), 0.5, 0.5, dx * 0.5, dy * 0.5), rng, fl)
        a1 = ease((t - 0.25) / 0.3)
        a2 = ease((t - 0.55) / 0.3)
        frame = compose(win, "ВОРОЖБИТОВ · ДРОП 001", "ДВА ПРИНТА", "ДВА ПРИНТА", ease((t - 0.2) / 0.3), 1.0, title_max=110)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        # подписи-ярлыки под каждой футболкой
        for x, label, sub, a in ((W * 0.27, "ПЕРЕД", "СИЛА И ЧЕСТЬ", a1), (W * 0.73, "СПИНА", "МЕЧ · ВВ", a2)):
            f, fs = caption(30), caption(22)
            tw = max(tracked_w(label, f, 0.3), tracked_w(sub, fs, 0.22))
            y = IY + IH - 96
            d.rectangle((x - tw / 2 - 22, y - 42, x + tw / 2 + 22, y + 44), fill=rgba(BLACK, 0.72 * a))
            draw_tracked(d, x, y, label, f, rgba(CREAM, a), tr=0.3, align="center")
            draw_tracked(d, x, y + 34, sub, fs, rgba(DIM, a), tr=0.22, align="center")
        yield Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")


def draw_sword(d: ImageDraw.ImageDraw, cx: float, top: float, length: float, progress: float, a: float,
               col=CREAM, lw: int = 3) -> None:
    """Меч остриём вниз, рисуется сверху вниз по progress 0..1 (как на спине футболки)."""
    pommel_y = top
    grip_top, grip_bot = top + 26, top + 96
    guard_y = grip_bot + 6
    blade_top, blade_bot = guard_y + 10, top + length
    fill = rgba(col, a)
    # прогресс: навершие → рукоять → гарда → клинок
    if progress > 0.0:
        d.ellipse((cx - 16, pommel_y - 16, cx + 16, pommel_y + 16), outline=fill, width=lw)
        d.ellipse((cx - 5, pommel_y - 5, cx + 5, pommel_y + 5), fill=fill)
    if progress > 0.08:
        y = grip_top + (grip_bot - grip_top) * min(1, (progress - 0.08) / 0.12)
        d.rectangle((cx - 9, grip_top, cx + 9, y), outline=fill, width=lw)
        for k in range(1, 5):
            yy = grip_top + k * 14
            if yy < y:
                d.line((cx - 9, yy, cx + 9, yy), fill=fill, width=2)
    if progress > 0.2:
        half = 78 * min(1, (progress - 0.2) / 0.1)
        d.rectangle((cx - half, guard_y - 8, cx + half, guard_y + 8), fill=fill)
    if progress > 0.3:
        pb = min(1, (progress - 0.3) / 0.7)
        y = blade_top + (blade_bot - blade_top) * pb
        wtop, wbot = 13.0, 3.5
        wy = wtop + (wbot - wtop) * pb
        pts = [(cx - wtop, blade_top), (cx + wtop, blade_top), (cx + wy, y - (26 if pb >= 1 else 0)), (cx, y), (cx - wy, y - (26 if pb >= 1 else 0))]
        d.polygon(pts, outline=fill, width=lw)
        d.line((cx, blade_top + 8, cx, y - 34), fill=rgba(col, a * 0.75), width=2)  # дол


def seg_outro(rng: np.random.Generator):
    n = int(OUTRO_SEC * FPS)
    weave = Weave(rng)
    for i in range(n):
        t = i / FPS
        dx, dy, fl = weave.step()
        base = np.full((IH, IW, 3), 0.035, dtype=np.float32)
        win = film_grade(Image.fromarray((base * 255).astype(np.uint8), "RGB"), rng, fl)
        frame = Image.new("RGB", (W, H), BLACK)
        frame.paste(win, (0, IY), gate_mask())
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, IY, IW - 1, IY + IH - 1), radius=22, outline=rgba(CREAM, 0.22), width=2)
        cx = W / 2 + dx * 0.5
        # монограмма ВВ с точками — как на спине
        a0 = ease((t - 0.1) / 0.3)
        if a0 > 0:
            f = ustav(64)
            d.text((cx, 560 + dy * 0.5), "ВВ", font=f, fill=rgba(CREAM, a0 * fl), anchor="ms")
            for sx in (-52, 52):
                d.ellipse((cx + sx - 4, 540 + dy * 0.5 - 4, cx + sx + 4, 540 + dy * 0.5 + 4), fill=rgba(CREAM, a0))
        # меч рисуется 0.25→1.35 с
        pr = ease((t - 0.25) / 1.1)
        if pr > 0:
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw_sword(ImageDraw.Draw(glow), cx, 610 + dy * 0.5, 560, pr, 0.8, col=(255, 225, 190), lw=7)
            layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)))
            d = ImageDraw.Draw(layer)
            draw_sword(d, cx, 610 + dy * 0.5, 560, pr, min(1.0, fl))
        # титр
        a1, s1 = title_anim(t, 1.5)
        if a1 > 0:
            size = int(fit_ustav("СИЛА И ЧЕСТЬ", 118, W - 140) * s1)
            tf = ustav(size)
            tw = tf.getlength("СИЛА И ЧЕСТЬ")
            d.text(((W - tw) / 2 + dx * 0.5 + 3, 1330 + 4), "СИЛА И ЧЕСТЬ", font=tf, fill=rgba((0, 0, 0), 0.8 * a1), anchor="ls")
            d.text(((W - tw) / 2 + dx * 0.5, 1330), "СИЛА И ЧЕСТЬ", font=tf, fill=rgba(CREAM, a1 * fl), anchor="ls")
        a2 = ease((t - 1.95) / 0.35)
        if a2 > 0:
            draw_tracked(d, W / 2, 1420, "ВОРОЖБИТОВ  ·  ДРОП 001  ·  2026", caption(28), rgba(DIM, a2), tr=0.32, align="center")
        a3 = ease((t - 2.3) / 0.35)
        if a3 > 0:
            draw_tracked(d, W / 2, 1560, "ТИРАЖ ОГРАНИЧЕН", caption(24), rgba(BLOOD, a3), tr=0.42, align="center")
        out = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
        if t > OUTRO_SEC - 0.5:  # затемнение
            k = 1 - (t - (OUTRO_SEC - 0.5)) / 0.5
            out = Image.fromarray((np.asarray(out, dtype=np.float32) * k).astype(np.uint8), "RGB")
        yield out


# ---------------------------------------------------------------------- план
def plan():
    """Список (имя, кадров). Жёсткие склейки — без перекрытий."""
    segs = [("intro", int(INTRO_SEC * FPS))]
    segs += [(f"shot{i + 1}", int(s[2] * FPS)) for i, s in enumerate(SHOTS)]
    segs.append(("slip", SLIP_FRAMES))
    segs.append(("montage", int(MONTAGE_SEC * FPS) * len(MONTAGE)))
    segs.append(("hero", int(HERO[2] * FPS)))
    segs.append(("flat", int(FLAT_SEC * FPS)))
    segs.append(("outro", int(OUTRO_SEC * FPS)))
    starts = {}
    pos = 0
    for name, n in segs:
        starts[name] = pos
        pos += n
    return segs, starts, pos


def segments(rng: np.random.Generator):
    yield "intro", seg_intro(rng)
    for i, spec in enumerate(SHOTS):
        yield f"shot{i + 1}", seg_shot(spec, rng, i + 1, len(SHOTS) + 1)
    yield "slip", seg_slip(rng)
    yield "montage", seg_montage(rng)
    yield "hero", seg_hero(rng)
    yield "flat", seg_flatlay(rng)
    yield "outro", seg_outro(rng)


# --------------------------------------------------------------------- звук
SR = 44100


def sos(kind, f, order=2):
    return butter(order, f, kind, fs=SR, output="sos")


def env_exp(n: int, k: float) -> np.ndarray:
    return np.exp(-np.arange(n) / SR * k).astype(np.float32)


def taiko(rng, vel=1.0, low=42.0) -> np.ndarray:
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    f = low + 85 * np.exp(-t * 16)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 4.2)
    skin = sosfilt(sos("low", 1800), rng.normal(0, 1, n)) * np.exp(-t * 55) * 0.6
    return ((body + skin) * vel).astype(np.float32)


def anvil(rng, vel=1.0, f0=1480.0) -> np.ndarray:
    n = int(1.4 * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float32)
    for r, a in ((1.0, 1.0), (2.76, 0.55), (5.40, 0.3), (8.93, 0.16)):
        out += (a * np.sin(2 * np.pi * f0 * r * t) * np.exp(-t * (5 + r * 1.8))).astype(np.float32)
    out += sosfilt(sos("band", [2500, 9000]), rng.normal(0, 1, n)).astype(np.float32) * np.exp(-t * 90) * 0.8
    return out * 0.32 * vel


def shing(rng, vel=1.0) -> np.ndarray:
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    noise = rng.normal(0, 1, n)
    out = np.zeros(n, dtype=np.float32)
    chunks = 12
    for c in range(chunks):
        lo = 1800 + (7000 - 1800) * c / chunks
        seg = slice(c * n // chunks, (c + 1) * n // chunks)
        out[seg] = sosfilt(sos("band", [lo, lo * 1.5]), noise)[seg]
    out *= np.exp(-t * 3.2) * (1 - np.exp(-t * 60))
    tone = np.sin(2 * np.pi * np.cumsum(2400 + 3200 * (1 - np.exp(-t * 4))) / SR) * np.exp(-t * 5) * 0.25
    return (out * 0.5 + tone).astype(np.float32) * vel


def pad(total: float, rng) -> tuple[np.ndarray, np.ndarray]:
    n = int(total * SR)
    t = np.arange(n, dtype=np.float32) / SR
    notes = (73.42, 110.0, 146.83, 174.61)  # D2 A2 D3 F3 — ре минор
    chans = []
    for det in (-0.0028, 0.0031):
        sig = np.zeros(n, dtype=np.float32)
        for f in notes:
            for k in range(1, 9):
                sig += (np.sin(2 * np.pi * f * (1 + det) * k * t + rng.uniform(0, 6.28)) / k * (1.0 if k % 2 else 0.55)).astype(np.float32)
        sig = sosfilt(sos("low", 900), sig).astype(np.float32)
        formant = sosfilt(sos("band", [420, 1400]), sig).astype(np.float32) * 0.9
        sig = sig + formant
        sig *= (0.9 + 0.1 * np.sin(2 * np.pi * 0.13 * t + (0 if det < 0 else 1.3))).astype(np.float32)
        chans.append(sig / (np.abs(sig).max() + 1e-6))
    return chans[0], chans[1]


def wind(total: float, rng) -> np.ndarray:
    n = int(total * SR)
    w = np.cumsum(rng.normal(0, 1, n)).astype(np.float32)
    w = sosfilt(sos("high", 25), w)
    w = sosfilt(sos("low", 700), w).astype(np.float32)
    mod = sosfilt(sos("low", 0.25), rng.normal(0, 1, n)).astype(np.float32)
    mod = 0.55 + 0.45 * mod / (np.abs(mod).max() + 1e-6)
    w = w * mod
    return w / (np.abs(w).max() + 1e-6)


def projector(total: float, rng) -> np.ndarray:
    n = int(total * SR)
    out = np.zeros(n, dtype=np.float32)
    step = SR / 18.0
    click = sosfilt(sos("band", [1800, 5200]), rng.normal(0, 1, int(0.004 * SR))).astype(np.float32) * np.linspace(1, 0, int(0.004 * SR))
    k = 0.0
    while int(k) + len(click) < n:
        out[int(k):int(k) + len(click)] += click * rng.uniform(0.6, 1.0)
        k += step
    hum = 0.25 * np.sin(2 * np.pi * 50 * np.arange(n) / SR).astype(np.float32)
    return (out / (np.abs(out).max() + 1e-6)) + hum * 0.2


def place(buf: np.ndarray, sig: np.ndarray, t0: float, gain: float = 1.0) -> None:
    i0 = int(t0 * SR)
    if i0 >= len(buf):
        return
    seg = sig[: len(buf) - i0]
    buf[i0:i0 + len(seg)] += seg * gain


def build_audio(starts: dict, total_frames: int, path: Path) -> None:
    rng = np.random.default_rng(11)
    total = total_frames / FPS
    n = int((total + 1.0) * SR)
    t = np.arange(n, dtype=np.float32) / SR
    L = np.zeros(n, dtype=np.float32)
    R = np.zeros(n, dtype=np.float32)
    s = {k: v / FPS for k, v in starts.items()}
    t_montage, t_hero, t_flat, t_outro = s["montage"], s["hero"], s["flat"], s["outro"]

    # --- пад: вход 0→3 с, уходит перед нарезкой, возвращается на герое тише
    pl, pr = pad(total + 1.0, rng)
    env = np.ones(n, dtype=np.float32)
    env *= np.clip(t / 3.0, 0, 1)
    env *= 1 - np.clip((t - (s["slip"] - 0.6)) / 0.6, 0, 1) * 1.0
    env += np.clip((t - t_hero) / 0.8, 0, 1) * 0.6 * (1 - np.clip((t - (total - 1.4)) / 1.0, 0, 1))
    L += pl * env * 0.34
    R += pr * env * 0.34

    # --- ветер (панорама плывёт) и проектор
    wnd = wind(total + 1.0, rng)
    panl = 0.5 + 0.5 * np.sin(2 * np.pi * 0.07 * t)
    wenv = 1 - np.clip((t - t_outro) / 1.5, 0, 1) * 0.7
    L += wnd * panl * 0.26 * wenv
    R += wnd * (1 - panl) * 0.26 * wenv
    prj = projector(total + 1.0, rng)
    prj_env = np.clip(t / 0.15, 0, 1) * (1 - np.clip((t - (total - 0.6)) / 0.4, 0, 1))
    L += prj * 0.07 * prj_env
    R += prj * 0.07 * prj_env

    # --- барабан: пульс 60 bpm + удар на каждой склейке
    beat = 1.0
    k = 1
    while k * beat < t_montage - 0.3:
        tk = k * beat
        vel = 0.22 if k % 2 else 0.34
        sig = taiko(rng, vel)
        place(L, sig, tk, 0.9)
        place(R, sig, tk, 0.9)
        k += 1
    cut_times = [s[f"shot{i + 1}"] for i in range(len(SHOTS))] + [t_hero, t_flat, t_outro]
    for i, ct in enumerate(cut_times):
        sig = taiko(rng, 1.0 if i in (0, len(cut_times) - 3) else 0.75, low=40 if i == len(cut_times) - 3 else 44)
        place(L, sig, ct)
        place(R, sig, ct)
    # нарезка: дробь по каждому кадру, нарастающая
    per = MONTAGE_SEC
    for j in range(len(MONTAGE)):
        sig = taiko(rng, 0.45 + 0.09 * j, low=50)
        place(L if j % 2 == 0 else R, sig, t_montage + j * per, 1.0)
        place(R if j % 2 == 0 else L, sig, t_montage + j * per, 0.55)
    # райзер к герою
    rl = int(1.6 * SR)
    riser = sosfilt(sos("high", 300), rng.normal(0, 1, rl)).astype(np.float32) * (np.linspace(0, 1, rl) ** 2.4) * 0.35
    place(L, riser, t_hero - 1.6)
    place(R, riser, t_hero - 1.6)

    # --- металл: наковальня на «ХАРАКТЕР», шинг на «МЕЧ», на герое и на мече в финале
    t_forge = s["shot6"]
    t_sword = s["shot7"]
    place(L, anvil(rng, 1.0), t_forge + 0.04, 0.8)
    place(R, anvil(rng, 0.8, 1520), t_forge + 0.05, 1.0)
    place(L, anvil(rng, 0.6, 1400), t_forge + 0.62, 0.9)
    place(R, anvil(rng, 0.6, 1400), t_forge + 0.62, 0.6)
    for tt, g in ((t_sword + 0.05, 0.55), (t_hero + 0.5, 0.5), (t_outro + 0.3, 0.7)):
        sg = shing(rng)
        place(L, sg, tt, g * 0.8)
        place(R, sg, tt + 0.012, g)
    # финальный удар под титр и тишина
    fin = taiko(rng, 1.0, low=38)
    place(L, fin, t_outro + 1.5)
    place(R, fin, t_outro + 1.5)

    # --- мастер: убрать лишний суб-бас, мягкий сатуратор
    hp = sos("high", 32, order=2)
    for ch in (L, R):
        ch[:] = sosfilt(hp, ch).astype(np.float32)
        low = sosfilt(sos("low", 120), ch).astype(np.float32)
        ch[:] = ch - low * 0.35            # -3.7 дБ ниже 120 Гц
        ch[:] = np.tanh(ch * 1.35)
    end = int(total * SR)
    fade = int(1.2 * SR)
    for ch in (L, R):
        ch[end - fade:end] *= np.linspace(1, 0, fade)
        ch[end:] = 0
        ch[: int(0.05 * SR)] *= np.linspace(0, 1, int(0.05 * SR))
    peak = max(np.abs(L).max(), np.abs(R).max(), 1e-6)
    L *= 0.89 / peak
    R *= 0.89 / peak
    pcm = np.empty(2 * n, dtype=np.int16)
    pcm[0::2] = (L * 32767).astype(np.int16)
    pcm[1::2] = (R * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


# ------------------------------------------------------------------ энкодер
class FrameSink:
    def __init__(self, out: Path, audio: Path, total_frames: int):
        self.n = 0
        duration = f"{total_frames / FPS:.3f}"
        self.proc = subprocess.Popen(
            [
                FFMPEG, "-y", "-loglevel", "warning",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "pipe:0",
                "-i", str(audio),
                "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p",
                "-threads", "2", "-x264-params", "rc-lookahead=10:bframes=2:ref=2:sync-lookahead=0:threads=2",
                "-thread_queue_size", "8",
                "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "192k", "-t", duration,   # без -shortest: ffmpeg 7 иначе копит кадры → OOM
                str(out),
            ],
            stdin=subprocess.PIPE, stderr=open(OUT_DIR / "_ffmpeg_v2.log", "w"), bufsize=0,
        )

    def push(self, frame: Image.Image) -> None:
        self.proc.stdin.write(frame.tobytes())
        self.n += 1

    def close(self) -> int:
        self.proc.stdin.close()
        return self.proc.wait()


def main() -> None:
    rng = np.random.default_rng(3)
    segs, starts, total_frames = plan()
    print(f"planned: {total_frames / FPS:.1f}s, segments: {[(k, round(v / FPS, 2)) for k, v in starts.items()]}", flush=True)
    audio = OUT_DIR / "_film.wav"
    build_audio(starts, total_frames, audio)
    sink = FrameSink(OUT_FILE, audio, total_frames)
    flash_after = {starts[f"shot{i + 1}"] for i in range(len(SHOTS))} | {starts["hero"], starts["flat"]}
    for name, gen in segments(rng):
        for j, fr in enumerate(gen):
            if sink.n in flash_after and j == 0:
                arr = np.asarray(fr, dtype=np.float32)
                fr = Image.fromarray(np.clip(arr * 0.35 + np.array(CREAM, dtype=np.float32) * 0.65, 0, 255).astype(np.uint8), "RGB")
            elif (sink.n - 1) in flash_after and j == 1:
                arr = np.asarray(fr, dtype=np.float32)
                fr = Image.fromarray(np.clip(arr * 0.75 + np.array(CREAM, dtype=np.float32) * 0.25, 0, 255).astype(np.uint8), "RGB")
            sink.push(fr)
        print(f"{name}: done, frames {sink.n}", flush=True)
    code = sink.close()
    audio.unlink(missing_ok=True)
    if code != 0:
        raise SystemExit(f"ffmpeg exited with {code}")
    print(f"frames: {sink.n} ({sink.n / FPS:.1f}s) -> {OUT_FILE} {OUT_FILE.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
