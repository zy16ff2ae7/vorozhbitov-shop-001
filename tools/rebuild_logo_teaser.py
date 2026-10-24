#!/usr/bin/env python3
"""Build an 18-second teaser version from the corrected product artwork.

The previous teaser and its audio are preserved.  This is a montage of still
visualisations, not a claim of new live footage.  Requires Pillow, NumPy-free
image operations only, and FFmpeg (or imageio-ffmpeg).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
ASSETS = MINIAPP / "assets"
APPROVAL = ROOT / "approval"
REGULAR = MINIAPP / "fonts/sila-chest-400.woff2"
BOLD = MINIAPP / "fonts/sila-chest-700.woff2"
SOURCE_VIDEO = ASSETS / "video/drop-vzglyad-18s.mp4"
OUTPUT_VIDEO = ASSETS / "video/drop-vzglyad-18s-v2.mp4"
OUTPUT_THUMB = ASSETS / "video/drop-vzglyad-18s-v2-thumb.jpg"
OUTPUT_SHEET = APPROVAL / "drop-vzglyad-logo-v2-18s-contact-sheet.jpg"
BG = (9, 9, 11)
INK = (244, 238, 230)
MUTED = (165, 159, 151)
RED = (225, 6, 0)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(BOLD if bold else REGULAR), size)


def ffmpeg_executable() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def dual_shirt() -> Image.Image:
    canvas = Image.new("RGB", (1080, 590), (18, 18, 18))
    for index, name in enumerate(("tee-sich-01-v3.jpg", "tee-sich-05-v3.jpg")):
        image = Image.open(ASSETS / "spin" / name).convert("RGB")
        image = image.crop((300, 0, 780, 590)).resize((480, 590), Image.Resampling.LANCZOS)
        canvas.paste(image, (60 + index * 480, 0))
    return canvas


def wordmark(canvas: Image.Image, y: int) -> None:
    mark = Image.open(ASSETS / "brand/sila-chest-wordmark.png").convert("RGBA")
    mark.thumbnail((610, 105), Image.Resampling.LANCZOS)
    canvas.alpha_composite(mark, ((720 - mark.width) // 2, y))


def scene_card(asset: str, title: str, subtitle: str, index: int, portrait=False, big=False) -> Image.Image:
    if asset == "dual":
        source = dual_shirt()
    else:
        source = Image.open(ASSETS / asset).convert("RGB")
    canvas = Image.new("RGB", (720, 1280), BG)

    if portrait:
        main = ImageOps.fit(source, (680, 990), Image.Resampling.LANCZOS, centering=(0.5, 0.34))
        canvas.paste(main, (20, 40))
    else:
        background = ImageOps.fit(source, (720, 900), Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(24))
        background = ImageEnhance.Brightness(background).enhance(0.32)
        canvas.paste(background, (0, 70))
        main = ImageOps.contain(source, (680, 630), Image.Resampling.LANCZOS)
        canvas.paste(main, ((720 - main.width) // 2, 190 + (630 - main.height) // 2))

    # Blend the visual into the title area without hiding garment details.
    gradient = Image.new("RGBA", (720, 620), (0, 0, 0, 0))
    pixels = gradient.load()
    for y in range(620):
        alpha = round(255 * max(0.0, (y - 80) / 540) ** 1.5)
        for x in range(720):
            pixels[x, y] = (*BG, alpha)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), Image.new("RGBA", (720, 1280), (*BG, 0)))
    canvas.alpha_composite(gradient, (0, 660))
    draw = ImageDraw.Draw(canvas)
    draw.text((40, 42), "СИЛА И ЧЕСТЬ  /  001", font=font(18, True), fill=INK)
    draw.line((40, 72, 104, 72), fill=RED, width=3)

    draw.text((42, 902), f"{index:02d}  /  ПЕРВЫЙ ДРОП", font=font(17, True), fill=RED)
    if big:
        wordmark(canvas, 946)
    else:
        draw.text((42, 944), title, font=font(47, True), fill=INK)
    draw.text((44, 1062), subtitle, font=font(21), fill=MUTED)
    draw.line((44, 1148, 91, 1148), fill=RED, width=3)
    draw.text((676, 1212), f"{index:02d}—09", font=font(15), fill=MUTED, anchor="ra")
    return canvas.convert("RGB")


def build_sheet(scenes: list[Image.Image]) -> None:
    canvas = Image.new("RGB", (792, 1550), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((48, 30), "СИЛА И ЧЕСТЬ  /  ПЕРВЫЙ ДРОП 001", font=font(23, True), fill=RED)
    draw.text((48, 66), "ФИЛЬМ ВЫПУСКА", font=font(44, True), fill=INK)
    draw.text((48, 116), "18 СЕК · 9 СЦЕН · НОВАЯ МОНОГРАММА · НА СОГЛАСОВАНИЕ", font=font(20), fill=MUTED)
    for index, scene in enumerate(scenes):
        frame = scene.resize((216, 384), Image.Resampling.LANCZOS)
        x = 48 + (index % 3) * 240
        y = 166 + (index // 3) * 422
        canvas.paste(frame, (x, y))
        draw.text((x, y + 389), f"{index * 2:02d}–{index * 2 + 2:02d} СЕК", font=font(18, True), fill=INK)
    draw.line((48, 1442, 744, 1442), fill=(50, 48, 46), width=1)
    draw.text((48, 1460), "Статус: на согласование. Монтаж предметных кадров, не новая живая съёмка.", font=font(18), fill=MUTED)
    canvas.save(OUTPUT_SHEET, quality=94, subsampling=0, optimize=True)


def main() -> None:
    specifications = [
        ("drop/tee-gym-retouched-final.jpg", "СИЛА И ЧЕСТЬ", "ОДЕЖДА С ХАРАКТЕРОМ", True, True),
        ("spin/tee-sich-01-v3.jpg", "СИЛА И ЧЕСТЬ", "ФУТБОЛКА · ПЕРВЫЙ ТИРАЖ", False, False),
        ("spin/tee-sich-05-v3.jpg", "УЗКИЙ КЛИНОК", "МОНОГРАММА · ПОТЁРТАЯ ФАКТУРА", False, False),
        ("drop/tee-detail-v5.jpg", "ДЕТАЛИ", "ФИРМЕННАЯ НАШИВКА", False, False),
        ("spin/tee-vzglyad-01-v2.jpg", "ВЗГЛЯД", "ФУТБОЛКА · ПРЕДЗАКАЗ", False, False),
        ("drop/tee-vzglyad-detail-v2.jpg", "СИЛА И ЧЕСТЬ", "ГРАФИКА · ЗНАК · МОНОГРАММА", False, False),
        ("spin/tee-vzglyad-05-v2.jpg", "КРУГ ЦЕННОСТЕЙ", "СИЛА · ЧЕСТЬ · ВЕРНОСТЬ", False, False),
        ("dual", "ДВЕ ФУТБОЛКИ", "ОДИН ДРОП · ОДИН ЗНАК", False, False),
        ("drop/hero-sila-chest-v2.jpg", "СИЛА И ЧЕСТЬ", "ПЕРВЫЙ ДРОП · 001", True, True),
    ]
    scenes = [scene_card(asset, title, subtitle, index + 1, portrait, big) for index, (asset, title, subtitle, portrait, big) in enumerate(specifications)]
    build_sheet(scenes)
    scenes[4].save(OUTPUT_THUMB, quality=93, subsampling=0)

    with tempfile.TemporaryDirectory(prefix="logo-teaser-") as directory:
        command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y"]
        for index, scene in enumerate(scenes):
            path = Path(directory) / f"scene-{index:02d}.jpg"
            scene.save(path, quality=95, subsampling=0)
            command += ["-i", str(path)]
        command += ["-i", str(SOURCE_VIDEO)]
        filters = []
        labels = []
        for index in range(9):
            filters.append(
                f"[{index}:v]zoompan=z='min(zoom+0.00055,1.033)':x='iw/2-(iw/zoom/2)'"
                f":y='ih/2-(ih/zoom/2)':d=60:s=720x1280:fps=30,setsar=1,"
                f"fade=t=in:st=0:d=0.10,fade=t=out:st=1.90:d=0.10[v{index}]"
            )
            labels.append(f"[v{index}]")
        filters.append("".join(labels) + "concat=n=9:v=1:a=0[video]")
        command += [
            "-filter_complex", ";".join(filters), "-map", "[video]", "-map", "9:a:0?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-t", "18", "-movflags", "+faststart", str(OUTPUT_VIDEO),
        ]
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
