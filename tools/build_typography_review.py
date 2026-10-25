#!/usr/bin/env python3
"""Compatibility entry point for the current versioned logo review builders.

The maintained implementations live in ``rebuild_logo_media.py`` and
``rebuild_logo_teaser.py``.  No production size chart or customer approval is
inferred: every sheet keeps the explicit review status.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
APPROVAL = ROOT / "approval"
REGULAR = MINIAPP / "fonts/sila-chest-400.woff2"
BOLD = MINIAPP / "fonts/sila-chest-700.woff2"
INK = (244, 238, 230)
MUTED = (165, 159, 151)
RED = (225, 6, 0)
BG = (10, 10, 12)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(BOLD if bold else REGULAR), size)


def contain_on_dark(path: Path, size: tuple[int, int]) -> Image.Image:
    source = Image.open(path).convert("RGB")
    image = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
    cell = Image.new("RGB", size, BG)
    cell.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return cell


def product_sheet(product: str, title: str, prefix: str, detail: str, video: str) -> None:
    canvas = Image.new("RGB", (1568, 980), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((48, 32), "СИЛА И ЧЕСТЬ  /  ПЕРВЫЙ ДРОП 001", font=font(25, True), fill=RED)
    draw.text((48, 70), title, font=font(56, True), fill=INK)
    draw.text((48, 130), "ФОТО И ВИДЕО · НОВАЯ ТИПОГРАФИКА · НА СОГЛАСОВАНИЕ", font=font(23), fill=MUTED)

    cells = [
        (f"spin/{prefix}01{'-v2' if prefix == 'tee-sich-' else ''}.jpg", "01 / СПЕРЕДИ"),
        (f"spin/{prefix}03{'-v2' if prefix == 'tee-sich-' else ''}.jpg", "02 / СБОКУ"),
        (f"spin/{prefix}05{'-v2' if prefix == 'tee-sich-' else ''}.jpg", "03 / СЗАДИ"),
        (f"spin/{prefix}07{'-v2' if prefix == 'tee-sich-' else ''}.jpg", "04 / ДРУГОЙ БОК"),
        (detail, "05 / ДЕТАЛЬ"),
    ]
    positions = [(48 + (i % 3) * 496, 168 + (i // 3) * 342) for i in range(5)]
    for (asset, label), (x, y) in zip(cells, positions):
        canvas.paste(contain_on_dark(MINIAPP / "assets" / asset, (480, 270)), (x, y))
        draw.text((x, y + 280), label, font=font(23, True), fill=INK)

    x, y = 1040, 510
    video_bg = contain_on_dark(MINIAPP / "assets" / cells[0][0], (480, 270))
    video_bg = ImageEnhance.Brightness(video_bg).enhance(0.42)
    canvas.paste(video_bg, (x, y))
    draw.text((x + 240, y + 102), "360° ВИДЕО", font=font(42, True), fill=INK, anchor="mm")
    draw.text((x + 240, y + 154), "MP4 · 4 СЕК · БЕЗ ЗВУКА", font=font(22), fill=INK, anchor="mm")
    draw.text((x, y + 280), "06 / 360° ВИДЕО", font=font(23, True), fill=RED)

    draw.line((48, 860, 1520, 860), fill=(50, 48, 46), width=1)
    draw.text((48, 884), "Проверить: принт, монограмму, нашивку, ракурсы и общий визуальный стиль.", font=font(21), fill=MUTED)
    draw.text((48, 922), "Статус: подготовлено к согласованию. Это не подтверждение производственной размерной сетки.", font=font(21), fill=MUTED)
    canvas.save(APPROVAL / f"{product}-contact-sheet.jpg", quality=94, subsampling=0, optimize=True)


def teaser_sheet() -> None:
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFMPEG is required for the teaser review sheet")
    video = MINIAPP / "assets/video/drop-vzglyad-18s.mp4"
    with tempfile.TemporaryDirectory(prefix="typography-review-") as directory:
        pattern = str(Path(directory) / "frame-%02d.jpg")
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
             "-vf", "fps=1/2,scale=216:384", "-frames:v", "9", pattern],
            check=True,
        )
        canvas = Image.new("RGB", (792, 1550), BG)
        draw = ImageDraw.Draw(canvas)
        draw.text((48, 30), "СИЛА И ЧЕСТЬ  /  ПЕРВЫЙ ДРОП 001", font=font(23, True), fill=RED)
        draw.text((48, 66), "ФИЛЬМ ВЫПУСКА", font=font(44, True), fill=INK)
        draw.text((48, 116), "18 СЕК · 9 СЦЕН · ОБНОВЛЁННЫЕ КАДРЫ И ТИТРЫ", font=font(20), fill=MUTED)
        for index in range(9):
            frame = Image.open(Path(directory) / f"frame-{index + 1:02d}.jpg").convert("RGB")
            x = 48 + (index % 3) * 240
            y = 166 + (index // 3) * 422
            canvas.paste(frame, (x, y))
            draw.text((x, y + 389), f"{index * 2:02d}–{index * 2 + 2:02d} СЕК", font=font(18, True), fill=INK)
        draw.line((48, 1442, 744, 1442), fill=(50, 48, 46), width=1)
        draw.text((48, 1460), "Статус: на согласование. Предметные рендеры, не заявленная живая съёмка.", font=font(18), fill=MUTED)
        canvas.save(APPROVAL / "drop-vzglyad-18s-contact-sheet.jpg", quality=94, subsampling=0, optimize=True)


def main() -> None:
    """Compatibility entry point for the current versioned logo review builders."""
    try:
        from .rebuild_logo_media import main as rebuild_media
        from .rebuild_logo_teaser import main as rebuild_teaser
    except ImportError:  # direct script execution
        from rebuild_logo_media import main as rebuild_media
        from rebuild_logo_teaser import main as rebuild_teaser

    rebuild_media()
    rebuild_teaser()


if __name__ == "__main__":
    main()
