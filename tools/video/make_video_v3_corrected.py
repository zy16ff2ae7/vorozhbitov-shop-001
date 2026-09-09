#!/usr/bin/env python3
"""Rebuild the original film cut using corrected campaign photographs.

Requires Python 3.10+, Pillow, NumPy, ffmpeg and ffprobe. The visual routines are
retained from make_video_v2_film.py; the original AAC stream is copied verbatim.
No source photographs, application settings, or old media files are modified.

Example:
    python3 tools/video/make_video_v3_corrected.py \
        --font-dir /path/to/sila-i-chest/video/fonts

With no mode flags both videos are rendered. --check-inputs validates only.
"""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from typing import Any

# Limit numerical worker pools before NumPy loads; ffmpeg is also single-threaded.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / "miniapp" / "assets"
USTAV = Path("RuslanDisplay.ttf")
CAPTION = Path("Oswald-Regular.ttf")

# Render the original 1080px typography, then downsample in the encoder.
W, H = 1080, 1920
FPS = 30
IW, IH, IY = 1080, 1440, 232
BAR_BOTTOM_CY = IY + IH + 124
BLACK, CREAM, DIM, BLOOD = (6, 5, 5), (236, 228, 212), (128, 120, 108), (168, 34, 30)

NIGHT = "drop/tee-night-v7.jpg"
CREW = "drop/tee-crew-v3.jpg"
ROOF = "drop/tee-roof-v3.jpg"
GYM = "drop/tee-gym-v3.jpg"
BACK = "drop/tee-back-v3.jpg"
FRONT_PRODUCT = "spin/tee-sich-01-v2.jpg"
BACK_PRODUCT = "spin/tee-sich-05-v2.jpg"
FLATLAY = "drop/tee-flatlay-v2.jpg"

# Keep the original sequence lengths so every existing audio cue stays aligned.
SHOTS = [
    (NIGHT, "НОЧЬ", 3.2, (0.50, 0.42), "push"),
    (CREW, "СВОИ", 2.8, (0.50, 0.45), "still"),
    (ROOF, "ГОРОД", 2.8, (0.50, 0.45), "still"),
    (GYM, "ЖЕЛЕЗО", 2.2, (0.50, 0.42), "punch"),
    (BACK, "ТЯГА", 1.8, (0.50, 0.40), "punch"),
    (GYM, "ХАРАКТЕР", 1.6, (0.50, 0.32), "punch"),
    (BACK, "МЕЧ", 1.4, (0.50, 0.40), "still"),
    (NIGHT, "СВОЙ ПУТЬ", 1.2, (0.50, 0.45), "still"),
]
SLIP_SRC = ROOF
MONTAGE = [
    (CREW, (0.50, 0.42)),
    (FRONT_PRODUCT, (0.50, 0.50)),
    (GYM, (0.50, 0.38)),
    (ROOF, (0.50, 0.38)),
    (NIGHT, (0.50, 0.40)),
    (BACK, (0.50, 0.38)),
]
HERO = (NIGHT, "СИЛА И ЧЕСТЬ", 3.6, (0.50, 0.42), "push")
INTRO_SEC, SLIP_FRAMES, MONTAGE_SEC, FLAT_SEC, OUTRO_SEC = 2.6, 5, 0.25, 2.6, 3.2
TEASER_FRAMES, WELCOME_FRAMES = 917, 378
WELCOME_SIZE = (810, 888)
WELCOME_SLOT_FRAMES, WELCOME_BLEND_FRAMES = 72, 9
WELCOME_SHOTS = [NIGHT, CREW, ROOF, GYM, BACK, FRONT_PRODUCT]
TEASER_SIZE_LIMIT = 6_000_000

# Film typography, grain, gate, dust, jitter and shot transitions from v2.

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


def contain_back(
    img: Image.Image, size: tuple[int, int], zoom: float = 1.0,
    dx: float = 0, dy: float = 0,
) -> Image.Image:
    """Keep the pull-up scene, whole person, blade and foreground details visible."""
    width, height = size
    # Adapt only this scene to both portrait gates. Preserve a margin for the
    # original film jitter instead of cropping hands, feet, hem or the mug.
    gentle_zoom = 1 + min(max(zoom - 1, 0), 0.18) / 6
    scale = min(width / img.width, height / img.height) * 0.96 * gentle_zoom
    fitted = img.resize(
        (round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS
    )
    frame = Image.new("RGB", size, BLACK)
    x = min(max(round((width - fitted.width) / 2 + dx), 0), width - fitted.width)
    y = min(max(round((height - fitted.height) / 2 + dy), 0), height - fitted.height)
    frame.paste(fitted, (x, y))
    return frame


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
        win = (
            contain_back(img, (IW, IH), zoom, dx, dy)
            if file == BACK else cover(img, zoom, *focus, dx, dy)
        )
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
            framed = (
                contain_back(img, (IW, IH), zoom)
                if file == BACK else cover(img, zoom, *focus)
            )
            win = film_grade(framed, rng, 1.0, bw=True)
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
    with Image.open(ROOT / FLATLAY) as source:
        photo = source.convert("RGB")
    # Both real garments must remain visible in the portrait film gate. A cover
    # crop would cut the outer sleeves off this square, side-by-side photograph.
    scale = min(IW * 0.96 / photo.width, IH * 0.96 / photo.height)
    fitted = photo.resize(
        (round(photo.width * scale), round(photo.height * scale)), Image.Resampling.LANCZOS
    )
    img = Image.new("RGB", (IW, IH), BLACK)
    img.paste(fitted, ((IW - fitted.width) // 2, (IH - fitted.height) // 2))
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



def seg_outro(rng: np.random.Generator) -> Iterator[Image.Image]:
    """Show the real corrected back instead of the old invented vector sword."""
    with Image.open(ROOT / BACK_PRODUCT) as source:
        product = source.convert("RGB")
    weave = Weave(rng)
    for i in range(int(OUTRO_SEC * FPS)):
        t = i / FPS
        dx, dy, flicker = weave.step()
        win = film_grade(cover(product, 1.01, 0.5, 0.5, dx, dy), rng, flicker)
        title_alpha, title_scale = title_anim(t, 1.5)
        frame = compose(
            win, "ВОРОЖБИТОВ · ДРОП 001", "СИЛА В ДЕТАЛЯХ",
            "СИЛА И ЧЕСТЬ", title_alpha, title_scale, title_max=118,
        )
        if t > OUTRO_SEC - 0.5:
            fade = (OUTRO_SEC - t) / 0.5
            frame = Image.fromarray(
                (np.asarray(frame, dtype=np.float32) * fade).astype(np.uint8)
            )
        yield frame


def plan() -> tuple[list[tuple[str, int]], dict[str, int], int]:
    """Return the unchanged 917-frame film timeline."""
    segments = [("intro", int(INTRO_SEC * FPS))]
    segments += [(f"shot{i + 1}", int(s[2] * FPS)) for i, s in enumerate(SHOTS)]
    segments += [
        ("slip", SLIP_FRAMES),
        ("montage", int(MONTAGE_SEC * FPS) * len(MONTAGE)),
        ("hero", int(HERO[2] * FPS)),
        ("flat", int(FLAT_SEC * FPS)),
        ("outro", int(OUTRO_SEC * FPS)),
    ]
    starts: dict[str, int] = {}
    position = 0
    for name, frames in segments:
        starts[name] = position
        position += frames
    return segments, starts, position


def film_segments(rng: np.random.Generator) -> Iterator[tuple[str, Iterator[Image.Image]]]:
    yield "intro", seg_intro(rng)
    for i, shot in enumerate(SHOTS):
        yield f"shot{i + 1}", seg_shot(shot, rng, i + 1, len(SHOTS) + 1)
    yield "slip", seg_slip(rng)
    yield "montage", seg_montage(rng)
    yield "hero", seg_hero(rng)
    yield "flat", seg_flatlay(rng)
    yield "outro", seg_outro(rng)


@dataclass(frozen=True)
class Configuration:
    assets_dir: Path
    output_dir: Path
    font_dir: Path | None
    audio_source: Path
    ffmpeg: str
    ffprobe: str
    teaser: bool
    welcome: bool
    check_inputs: bool


def probe(path: Path, binary: str) -> dict[str, Any]:
    completed = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def audio_hash(path: Path, binary: str) -> str:
    """Hash AAC packet payloads without decoding or re-encoding them."""
    completed = subprocess.run(
        [binary, "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "copy",
         "-f", "hash", "-hash", "sha256", "pipe:1"],
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


def has_faststart(path: Path) -> bool:
    """Check top-level MP4 atoms, without matching byte strings inside media."""
    size = path.stat().st_size
    with path.open("rb") as media:
        while media.tell() + 8 <= size:
            start = media.tell()
            length, kind = struct.unpack(">I4s", media.read(8))
            if length == 1:
                raw = media.read(8)
                if len(raw) != 8:
                    return False
                length = struct.unpack(">Q", raw)[0]
            elif length == 0:
                length = size - start
            if kind == b"moov":
                return True
            if kind == b"mdat" or length < 8 or start + length > size:
                return False
            media.seek(start + length)
    return False


def validate_output(path: Path, config: Configuration, *, teaser: bool) -> dict[str, Any]:
    info = probe(path, config.ffprobe)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    expected_size = (720, 1280) if teaser else WELCOME_SIZE
    expected_frames = TEASER_FRAMES if teaser else WELCOME_FRAMES
    if len(videos) != 1:
        raise RuntimeError(f"Expected one video stream: {path}")
    video = videos[0]
    if (
        video["codec_name"] != "h264"
        or (video["width"], video["height"]) != expected_size
        or video["r_frame_rate"] != "30/1"
        or int(video["nb_frames"]) != expected_frames
        or video["pix_fmt"] != "yuv420p"
    ):
        raise RuntimeError(f"Unexpected video format: {video}")
    if abs(float(video["duration"]) - expected_frames / FPS) > 0.002:
        raise RuntimeError(f"Unexpected video duration: {video['duration']}")
    if not has_faststart(path):
        raise RuntimeError(f"Missing MP4 faststart: {path}")
    if teaser:
        if len(audio) != 1 or audio[0]["codec_name"] != "aac":
            raise RuntimeError("Teaser must contain the original AAC stream")
        if path.stat().st_size >= TEASER_SIZE_LIMIT:
            raise RuntimeError("Teaser exceeds the 6 MB mobile size limit")
        if audio_hash(path, config.ffmpeg) != audio_hash(config.audio_source, config.ffmpeg):
            raise RuntimeError("Teaser AAC payload differs from the original")
    elif audio:
        raise RuntimeError("Welcome loop must be silent")
    return {
        "file": str(path), "frames": expected_frames, "size": expected_size,
        "duration": video["duration"], "bytes": path.stat().st_size,
        "audio": "original AAC, payload verified" if teaser else "none",
    }


class FrameSink:
    """Stream frames to one low-priority encoder and clean up on failure."""

    def __init__(self, output: Path, config: Configuration, *, teaser: bool) -> None:
        self.n = 0
        self.log = output.with_suffix(".log").open("w+")
        size = (W, H) if teaser else WELCOME_SIZE
        command = [
            config.ffmpeg, "-y", "-v", "warning", "-threads", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
            "-framerate", str(FPS), "-i", "pipe:0",
        ]
        if teaser:
            command += ["-threads", "1", "-i", str(config.audio_source)]
            # Source 1 contains the old video too: never rely on automatic selection.
            command += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
            command += ["-vf", "scale=720:1280:flags=lanczos"]
        else:
            command += ["-map", "0:v:0", "-an"]
        command += [
            "-c:v", "libx264", "-preset", "slow", "-crf", "24" if teaser else "23",
            "-maxrate", "1300k" if teaser else "1600k", "-bufsize", "1300k" if teaser else "1600k",
            "-pix_fmt", "yuv420p", "-threads", "1", "-filter_threads", "1",
            "-x264-params", "threads=1:lookahead-threads=1:sync-lookahead=0:keyint=60:min-keyint=30",
            "-profile:v", "high", "-level", "4.0", "-movflags", "+faststart", str(output),
        ]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=self.log)
        except BaseException:
            self.log.close()
            raise

    def push(self, frame: Image.Image) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Encoder input is closed")
        self.process.stdin.write(frame.tobytes())
        self.n += 1

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        result = self.process.wait()
        self.log.seek(0)
        log_text = self.log.read()
        self.log.close()
        if result:
            raise RuntimeError(f"ffmpeg exited with {result}: {log_text[-4000:]}")

    def abort(self) -> None:
        try:
            if self.process.poll() is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    self.process.wait()
        finally:
            try:
                if self.process.stdin is not None:
                    # A failed encoder can leave buffered bytes in the pipe.
                    # Cleanup must preserve the original rendering exception.
                    with suppress(OSError):
                        self.process.stdin.close()
            finally:
                if not self.log.closed:
                    self.log.close()


def render_teaser(config: Configuration, stage: Path) -> list[tuple[Path, Path]]:
    _, starts, total = plan()
    if total != TEASER_FRAMES:
        raise RuntimeError(f"Film timeline drifted: {total} frames")
    output = stage / "teaser-v3.mp4"
    poster = stage / "teaser-poster-v3.jpg"
    thumbnail = stage / "teaser-thumbnail-v3.jpg"
    sink = FrameSink(output, config, teaser=True)
    flashes = {starts[f"shot{i + 1}"] for i in range(len(SHOTS))}
    flashes |= {starts["hero"], starts["flat"]}
    try:
        for name, frames in film_segments(np.random.default_rng(3)):
            for i, frame in enumerate(frames):
                if sink.n in flashes and i == 0:
                    arr = np.asarray(frame, dtype=np.float32)
                    frame = Image.fromarray(
                        np.clip(arr * 0.35 + np.array(CREAM) * 0.65, 0, 255).astype(np.uint8)
                    )
                elif sink.n - 1 in flashes and i == 1:
                    arr = np.asarray(frame, dtype=np.float32)
                    frame = Image.fromarray(
                        np.clip(arr * 0.75 + np.array(CREAM) * 0.25, 0, 255).astype(np.uint8)
                    )
                if sink.n == 666:  # 22.2s, inside the long corrected hero shot.
                    frame.resize((720, 1280), Image.Resampling.LANCZOS).save(
                        poster, quality=93, optimize=True
                    )
                    frame.resize((180, 320), Image.Resampling.LANCZOS).save(
                        thumbnail, quality=90, optimize=True
                    )
                sink.push(frame)
            print(f"teaser {name}: {sink.n}/{total} frames", flush=True)
        sink.close()
    except BaseException:
        sink.abort()
        raise
    if sink.n != TEASER_FRAMES:
        raise RuntimeError(f"Unexpected rendered frame count: {sink.n}")
    print(json.dumps(validate_output(output, config, teaser=True), ensure_ascii=False), flush=True)
    return [
        (output, config.output_dir / output.name),
        (poster, config.output_dir / poster.name),
        (thumbnail, config.output_dir / thumbnail.name),
    ]


def welcome_shot(path: Path, index: int) -> Iterator[Image.Image]:
    """Small camera movements; generated campaign lighting stays unchanged."""
    with Image.open(path) as source:
        img = source.convert("RGB")
    width, height = WELCOME_SIZE
    iw, ih = img.size
    for frame in range(WELCOME_SLOT_FRAMES):
        progress = ease(frame / (WELCOME_SLOT_FRAMES - 1))
        zoom = 1.0 + 0.035 * (progress if index % 2 else 1 - progress)
        if path == ROOT / BACK:
            yield contain_back(img, WELCOME_SIZE, zoom)
            continue
        scale = max(width / iw, height / ih) * zoom
        crop_width, crop_height = width / scale, height / scale
        center_y = 0.44 if index < 5 else 0.5
        x = (iw - crop_width) / 2
        y = min(max(center_y * ih - crop_height / 2, 0), ih - crop_height)
        yield img.resize(
            WELCOME_SIZE, Image.Resampling.LANCZOS,
            box=(x, y, x + crop_width, y + crop_height),
        )


def render_welcome(config: Configuration, stage: Path) -> list[tuple[Path, Path]]:
    output = stage / "welcome-loop-v3.mp4"
    poster = stage / "welcome-poster-v3.jpg"
    bot_welcome = stage / "welcome-v3.jpg"
    sink = FrameSink(output, config, teaser=False)
    blend_frames = WELCOME_BLEND_FRAMES
    first_head: list[Image.Image] = []
    tail: list[Image.Image] = []
    try:
        for index, name in enumerate(WELCOME_SHOTS):
            frames = iter(welcome_shot(config.assets_dir / name, index))
            head = [next(frames) for _ in range(blend_frames)]
            if index == 0:
                first_head = head
            else:
                for i, (old, new) in enumerate(zip(tail, head)):
                    sink.push(Image.blend(old, new, ease((i + 1) / blend_frames)))
            body: list[Image.Image] = []
            for frame in frames:
                body.append(frame)
                if len(body) > blend_frames:
                    ready = body.pop(0)
                    if sink.n == 0:
                        ready.save(poster, quality=93, optimize=True)
                        ready.save(bot_welcome, quality=93, optimize=True)
                    sink.push(ready)
            tail = body
            print(f"welcome shot {index + 1}/6: {sink.n} frames", flush=True)
        for i, (old, new) in enumerate(zip(tail, first_head)):
            sink.push(Image.blend(old, new, ease((i + 1) / blend_frames)))
        sink.close()
    except BaseException:
        sink.abort()
        raise
    if sink.n != WELCOME_FRAMES:
        raise RuntimeError(f"Unexpected welcome frame count: {sink.n}")
    print(json.dumps(validate_output(output, config, teaser=False), ensure_ascii=False), flush=True)
    return [
        (output, config.output_dir / output.name),
        (poster, config.output_dir / poster.name),
        (bot_welcome, config.assets_dir / bot_welcome.name),
    ]


def arguments() -> Configuration:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, help="Defaults to ASSETS_DIR/video")
    parser.add_argument("--font-dir", type=Path, help="Directory containing RuslanDisplay.ttf and Oswald-Regular.ttf")
    parser.add_argument("--audio-source", type=Path, help="Defaults to ASSETS_DIR/video/teaser.mp4")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--teaser", action="store_true", help="Render the film cut")
    parser.add_argument("--welcome", action="store_true", help="Render the silent welcome loop")
    parser.add_argument("--check-inputs", action="store_true", help="Validate sources without rendering")
    args = parser.parse_args()
    both = not (args.teaser or args.welcome)
    assets = args.assets_dir.resolve()
    return Configuration(
        assets, (args.output_dir or assets / "video").resolve(),
        args.font_dir.resolve() if args.font_dir else None,
        (args.audio_source or assets / "video" / "teaser.mp4").resolve(),
        args.ffmpeg, args.ffprobe, args.teaser or both, args.welcome or both, args.check_inputs,
    )


def validate_inputs(config: Configuration) -> None:
    global ROOT, USTAV, CAPTION
    ROOT = config.assets_dir
    for executable in (config.ffmpeg, config.ffprobe):
        if shutil.which(executable) is None:
            raise ValueError(f"Executable not found: {executable}")
    required = set(WELCOME_SHOTS) if config.welcome else set()
    if config.teaser:
        required.update({NIGHT, CREW, ROOF, GYM, BACK, FRONT_PRODUCT, BACK_PRODUCT, FLATLAY})
        if config.font_dir is None:
            raise ValueError("--font-dir is required for the teaser")
        USTAV = config.font_dir / "RuslanDisplay.ttf"
        CAPTION = config.font_dir / "Oswald-Regular.ttf"
        for font_path in (USTAV, CAPTION):
            ImageFont.truetype(str(font_path), 24)
        original = probe(config.audio_source, config.ffprobe)
        audio = [s for s in original["streams"] if s["codec_type"] == "audio"]
        if len(audio) != 1 or audio[0]["codec_name"] != "aac":
            raise ValueError("Audio source must contain exactly one AAC stream")
        if abs(float(audio[0]["duration"]) - TEASER_FRAMES / FPS) > 0.1:
            raise ValueError("Audio source does not match the original film duration")
        if config.audio_source == config.output_dir / "teaser-v3.mp4":
            raise ValueError("The original audio source must not be the output file")
    for name in sorted(required):
        with Image.open(config.assets_dir / name) as image:
            image.verify()
    print(f"Validated {len(required)} corrected photographs; modes: "
          f"teaser={config.teaser}, welcome={config.welcome}", flush=True)


def main() -> None:
    config = arguments()
    validate_inputs(config)
    if config.check_inputs:
        print(f"Film plan: {plan()[2]} frames; welcome plan: "
              f"{len(WELCOME_SHOTS) * (WELCOME_SLOT_FRAMES - WELCOME_BLEND_FRAMES)} frames")
        return
    if hasattr(os, "nice"):
        os.nice(10)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.welcome and config.output_dir.stat().st_dev != config.assets_dir.stat().st_dev:
        raise ValueError(
            "--output-dir and --assets-dir must be on the same filesystem "
            "to publish the welcome video and photo atomically per file"
        )
    # All selected outputs must validate before any existing v3 file is replaced.
    with tempfile.TemporaryDirectory(prefix=".video-v3-", dir=config.output_dir) as temporary:
        stage = Path(temporary)
        outputs: list[tuple[Path, Path]] = []
        if config.teaser:
            outputs.extend(render_teaser(config, stage))
        if config.welcome:
            outputs.extend(render_welcome(config, stage))
        for source, destination in outputs:
            source.replace(destination)
            print(f"Written: {destination}", flush=True)


if __name__ == "__main__":
    main()
