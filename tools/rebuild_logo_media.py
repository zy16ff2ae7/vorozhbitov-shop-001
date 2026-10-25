#!/usr/bin/env python3
"""Rebuild active 360° MP4s and approval sheets after the logo replacement.

Reads the current catalog, writes new versioned videos and new review sheets,
and never overwrites the prior videos or review boards.  Requires Pillow and
FFmpeg (``imageio-ffmpeg`` is used when ffmpeg is not on PATH).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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


def ffmpeg_executable() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def contain_on_dark(path: Path, size: tuple[int, int]) -> Image.Image:
    source = Image.open(path).convert("RGB")
    image = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
    if source.height > source.width * 1.2:
        # Portrait rotation sources need a full-bleed cell without cropping the
        # garment: a soft enlargement fills the sides, the sharp frame stays whole.
        cell = ImageOps.fit(source, size, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(18))
        cell = Image.blend(cell, Image.new("RGB", size, BG), 0.48)
    else:
        cell = Image.new("RGB", size, BG)
    cell.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return cell


def build_video(product: dict) -> None:
    """Render an exact eight-second loop with frame-aligned crossfades.

    Nine one-second inputs (eight angles plus the first angle) and eight
    0.125-second transitions produce 192 frames at 24 fps.  No zoom or pan is
    applied, so the garment remains stable and the loop closes on frame 01.
    """
    output = MINIAPP / product["video"]
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = [MINIAPP / asset for asset in product["spin"]]
    if len(frames) != 8:
        raise ValueError(f"{product['id']}: expected eight spin frames")
    sequence = [*frames, frames[0]]
    command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y"]
    for frame in sequence:
        command += ["-loop", "1", "-framerate", "24", "-t", "1", "-i", str(frame)]

    filters = []
    for index in range(len(sequence)):
        filters.append(
            f"[{index}:v]scale=1080:590:force_original_aspect_ratio=increase,"
            f"crop=1080:590,fps=24,format=yuv420p[v{index}]"
        )
    previous = "v0"
    for index in range(1, len(sequence)):
        output_label = f"x{index}"
        filters.append(
            f"[{previous}][v{index}]xfade=transition=fade:duration=0.125:"
            f"offset={index * 0.875:.3f},fps=24[{output_label}]"
        )
        previous = output_label
    filters.append(f"[{previous}]fps=24,format=yuv420p[video]")
    command += [
        "-filter_complex", ";".join(filters), "-map", "[video]",
        "-frames:v", "192", "-r", "24", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]
    subprocess.run(command, check=True)


def product_sheet(product: dict, output_name: str) -> None:
    canvas = Image.new("RGB", (1568, 980), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((48, 32), "СИЛА И ЧЕСТЬ  /  ПЕРВЫЙ ДРОП 001", font=font(25, True), fill=RED)
    draw.text((48, 70), product["name"], font=font(56, True), fill=INK)
    draw.text((48, 130), "ФОТО И ВИДЕО · НОВАЯ МОНОГРАММА · НА СОГЛАСОВАНИЕ", font=font(23), fill=MUTED)

    cells = [
        (product["images"][0], "01 / СПЕРЕДИ"),
        (product["images"][1], "02 / СБОКУ"),
        (product["images"][2], "03 / СЗАДИ"),
        (product["images"][3], "04 / ДРУГОЙ БОК"),
        (product["images"][4], "05 / ДЕТАЛЬ"),
    ]
    positions = [(48 + (i % 3) * 496, 168 + (i // 3) * 342) for i in range(5)]
    for (asset, label), (x, y) in zip(cells, positions):
        canvas.paste(contain_on_dark(MINIAPP / asset, (480, 270)), (x, y))
        draw.text((x, y + 280), label, font=font(23, True), fill=INK)

    x, y = 1040, 510
    video_bg = contain_on_dark(MINIAPP / cells[0][0], (480, 270))
    video_bg = ImageEnhance.Brightness(video_bg).enhance(0.42)
    canvas.paste(video_bg, (x, y))
    draw.text((x + 240, y + 102), "360° ВИДЕО", font=font(42, True), fill=INK, anchor="mm")
    draw.text((x + 240, y + 154), "MP4 · 8 СЕК · БЕЗ ЗВУКА", font=font(22), fill=INK, anchor="mm")
    draw.text((x, y + 280), "06 / 360° ВИДЕО", font=font(23, True), fill=RED)

    draw.line((48, 860, 1520, 860), fill=(50, 48, 46), width=1)
    review_note = (
        "Проверить: фактурный знак, надпись на спине, рукавную и нижнюю нашивки."
        if product["id"] == "tee-odin-v-pole-voin"
        else "Проверить: новый знак, сохранение клинка, кольца ценностей, текста и нашивок."
    )
    draw.text((48, 884), review_note, font=font(21), fill=MUTED)
    draw.text((48, 922), "Статус: подготовлено к согласованию. Исходные кадры и прежние версии сохранены.", font=font(21), fill=MUTED)
    canvas.save(APPROVAL / output_name, quality=94, subsampling=0, optimize=True)


def tag_sheet(product: dict) -> None:
    canvas = Image.new("RGB", (1200, 1040), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((42, 30), "СИЛА И ЧЕСТЬ  /  ПЕРВЫЙ ДРОП 001", font=font(23, True), fill=RED)
    draw.text((42, 66), "ЖЕТОН · НОВАЯ МОНОГРАММА", font=font(43, True), fill=INK)
    draw.text((42, 122), "ПРЕДМЕТНЫЕ РАКУРСЫ · НА СОГЛАСОВАНИЕ", font=font(21), fill=MUTED)
    labels = ["01 / ГРАВИРОВКА", "02 / РАКУРС", "03 / ЦЕПОЧКА", "04 / ДЕТАЛЬ"]
    for index, (asset, label) in enumerate(zip(product["spin"], labels)):
        x = 42 + (index % 2) * 568
        y = 166 + (index // 2) * 390
        canvas.paste(contain_on_dark(MINIAPP / asset, (548, 330)), (x, y))
        draw.text((x, y + 340), label, font=font(21, True), fill=INK)
    draw.line((42, 948, 1158, 948), fill=(50, 48, 46), width=1)
    draw.text((42, 970), "Старый тонкий знак заменён только на лицевой гравировке; металл и другие ракурсы сохранены.", font=font(19), fill=MUTED)
    canvas.save(APPROVAL / "tag-sila-i-chest-logo-v3-contact-sheet.jpg", quality=94, subsampling=0, optimize=True)


def main() -> None:
    APPROVAL.mkdir(exist_ok=True)
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    products = {product["id"]: product for product in catalog["products"]}
    for product_id, sheet_name in (
        ("tee-sila-i-chest", "tee-sila-i-chest-logo-v5-contact-sheet.jpg"),
        ("tee-vzglyad", "tee-vzglyad-logo-v4-contact-sheet.jpg"),
        ("tee-odin-v-pole-voin", "tee-odin-v-pole-voin-logo-v4-contact-sheet.jpg"),
    ):
        product = products[product_id]
        build_video(product)
        product_sheet(product, sheet_name)


if __name__ == "__main__":
    main()
