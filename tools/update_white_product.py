#!/usr/bin/env python3
"""Prepare the white «ОДИН В ПОЛЕ ВОИН» review board with the new artwork.

Uses the customer-supplied monogram through ``update_product_logos`` and the
same local Sila Chest font as the rest of the storefront.  The original board
is preserved.  Output is a review visualisation, not production approval.

Requires Pillow, NumPy and OpenCV (``opencv-python-headless``).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from .update_product_logos import replace_logo
except ImportError:  # direct ``python tools/update_white_product.py`` execution
    from update_product_logos import replace_logo

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
DROP = MINIAPP / "assets/drop"
SPIN = MINIAPP / "assets/spin"
SOURCE = DROP / "odin-v-pole-voin.jpg"
TARGET = DROP / "odin-v-pole-voin-v3.jpg"
FONT_REGULAR = MINIAPP / "fonts/sila-chest-400.woff2"
FONT_BOLD = MINIAPP / "fonts/sila-chest-700.woff2"
TEXT_LINES = [("ОДИН В ПОЛЕ", 0.30), ("ВОИН", 0.70)]
REVIEW_DIR = ROOT / "approval/brand-logo-review-2026-10-25-v2"
FRAME_DIR = REVIEW_DIR / "white-frames"
SOURCE_VIDEO = ROOT / "task_materials/chat_export/video_files/0818.mp4"
REVIEW_VIDEO = REVIEW_DIR / "odin-v-pole-voin-review.mp4"


def text_art(size: tuple[int, int], lines=TEXT_LINES, colour=(18, 18, 18, 255)) -> Image.Image:
    width, height = size
    scale = 3
    canvas = Image.new("RGBA", (width * scale, height * scale))
    draw = ImageDraw.Draw(canvas)
    y = 0
    for text, fraction in lines:
        target_height = round(height * fraction)
        font_size = target_height * scale
        while font_size > 5:
            font = ImageFont.truetype(str(FONT_BOLD), font_size)
            bounds = draw.textbbox((0, 0), text, font=font)
            if bounds[2] - bounds[0] <= width * scale and bounds[3] - bounds[1] <= target_height * scale:
                break
            font_size -= 2
        bounds = draw.textbbox((0, 0), text, font=font)
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.text(
            ((width * scale - text_width) // 2 - bounds[0], y * scale + (target_height * scale - text_height) // 2 - bounds[1]),
            text,
            font=font,
            fill=colour,
        )
        y += target_height
    return canvas.resize((width, height), Image.Resampling.LANCZOS)


def replace_text(image: Image.Image, box, lines=TEXT_LINES, old_box=None) -> Image.Image:
    x, y, width, height = box
    ox, oy, ow, oh = old_box or box
    pixels = np.asarray(image.convert("RGB")).copy()
    roi = pixels[oy : oy + oh, ox : ox + ow]
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    mask = (gray < 202).astype(np.uint8) * 255
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    full_mask = np.zeros(pixels.shape[:2], np.uint8)
    full_mask[oy : oy + oh, ox : ox + ow] = mask
    cleaned = cv2.inpaint(pixels, full_mask, 6, cv2.INPAINT_TELEA)
    result = Image.fromarray(cleaned).convert("RGBA")
    result.alpha_composite(text_art((width, height), lines), (x, y))
    return result


def cover_back_print(image: Image.Image) -> Image.Image:
    """Replace the large old print with clean cloth using Poisson blending."""
    pixels = np.asarray(image.convert("RGB")).copy()
    # Same-size clean area lower on the back, with no print or garment edge.
    x, y, width, height = 875, 145, 350, 240
    sx, sy = 875, 390
    patch = pixels[sy : sy + height, sx : sx + width].copy()
    mask = np.zeros((height, width), np.uint8)
    mask[8:-8, 8:-8] = 255
    mixed = cv2.seamlessClone(patch, pixels, mask, (x + width // 2, y + height // 2), cv2.NORMAL_CLONE)
    result = Image.fromarray(mixed).convert("RGBA")
    result.alpha_composite(text_art((330, 220)), (885, 155))
    return result


def label(text: str, width: int, height: int, angle: float) -> Image.Image:
    patch = Image.new("RGBA", (width * 3, height * 3), (13, 13, 13, 255))
    draw = ImageDraw.Draw(patch)
    size = height * 2
    while size > 8:
        font = ImageFont.truetype(str(FONT_BOLD), size)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= (width - 8) * 3:
            break
        size -= 2
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((width * 3 - (bounds[2] - bounds[0])) // 2 - bounds[0], (height * 3 - (bounds[3] - bounds[1])) // 2 - bounds[1]),
        text,
        font=font,
        fill=(232, 230, 222, 255),
    )
    return patch.resize((width, height), Image.Resampling.LANCZOS).rotate(angle, Image.Resampling.BICUBIC, expand=True)


def update_labels_and_footer(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    # Woven sleeve/hem labels: same content, local brand font.
    for patch, position in (
        (label("СИЛА И ЧЕСТЬ", 48, 16, -14), (598, 309)),
        (label("СИЛА И ЧЕСТЬ", 46, 16, 14), (737, 326)),
        (label("СИЛА И ЧЕСТЬ", 160, 58, -14), (350, 790)),
    ):
        result.alpha_composite(patch, position)

    draw = ImageDraw.Draw(result)
    columns = [
        (90, 185, "ПЛОТНЫЙ ХЛОПОК", ["240–260 GSM", "ПРЕМИУМ КАЧЕСТВО"]),
        (350, 200, "МОЩНЫЙ СМЫСЛ", ["ОДИН В ПОЛЕ ВОИН", "НАШ КОД"]),
        (630, 205, "СИМВОЛ БРАТСТВА", ["ЕДИНСТВО – НАША СИЛА"]),
        (915, 205, "СВОЯ ГОЛОВА", ["СВОИ ПРАВИЛА", "ЖИВИ ПО СВОЕМУ КОДУ"]),
        (1200, 195, "ФИРМЕННЫЙ СТИЛЬ", ["ЛАКОНИЧНО", "И ДОСТОЙНО"]),
    ]
    heading = ImageFont.truetype(str(FONT_BOLD), 20)
    body = ImageFont.truetype(str(FONT_REGULAR), 16)
    for x, width, title, lines in columns:
        draw.rectangle((x, 1029, x + width, 1107), fill=(11, 11, 11, 255))
        draw.text((x + 7, 1033), title, font=heading, fill=(202, 202, 198, 255))
        for index, line in enumerate(lines):
            draw.text((x + 7, 1060 + index * 18), line, font=body, fill=(174, 174, 170, 255))
    return result


def ffmpeg_executable() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise RuntimeError("Install imageio-ffmpeg or provide ffmpeg on PATH") from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def build_white_review_frames() -> None:
    """Edit ten angles extracted from the untouched customer video."""
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    logos = {
        1: (285, 450, 120, 210),
        2: (205, 450, 80, 210),
        9: (450, 450, 75, 200),
        10: (365, 450, 115, 210),
    }
    texts = {
        3: ((440, 435, 70, 260), (440, 435, 70, 260)),
        4: ((295, 435, 290, 270), (295, 435, 290, 270)),
        5: ((175, 440, 410, 260), (175, 440, 410, 260)),
        6: ((125, 435, 390, 270), (125, 435, 390, 270)),
        7: ((140, 440, 260, 260), (140, 440, 260, 260)),
        8: ((200, 435, 70, 260), (200, 435, 70, 260)),
    }
    hem_tags = {
        1: (505, 1060, 20, 40),
        2: (340, 1070, 25, 45),
        3: (155, 1060, 20, 40),
        10: (568, 1045, 18, 40),
    }
    with tempfile.TemporaryDirectory(prefix="odin-voin-frames-") as temporary:
        source_pattern = Path(temporary) / "source-%02d.jpg"
        subprocess.run(
            [
                ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(SOURCE_VIDEO),
                "-vf", "select='not(mod(n-6,30))',scale=720:1280",
                "-fps_mode", "vfr", "-q:v", "2", str(source_pattern),
            ],
            check=True,
        )
        for frame in range(1, 11):
            source = Path(temporary) / f"source-{frame:02d}.jpg"
            target = FRAME_DIR / f"odin-voin-{frame:02d}.jpg"
            if frame in logos:
                replace_logo(source, target, logos[frame], variant="dark", old="black")
            elif frame in texts:
                box, old_box = texts[frame]
                edited = replace_text(Image.open(source), box, old_box=old_box)
                if frame == 7:
                    edited = replace_text(edited, (375, 675, 25, 35), lines=[])
                edited.convert("RGB").save(target, quality=96, subsampling=0)
            else:
                shutil.copy2(source, target)
            if frame in hem_tags:
                temporary_frame = target.with_suffix(".working.jpg")
                shutil.copy2(target, temporary_frame)
                replace_logo(temporary_frame, target, hem_tags[frame], variant="light", old="shape")
                temporary_frame.unlink(missing_ok=True)

    sheet = Image.new("RGB", (1080, 768), (15, 15, 15))
    for index in range(10):
        frame = Image.open(FRAME_DIR / f"odin-voin-{index + 1:02d}.jpg")
        frame.thumbnail((216, 384), Image.Resampling.LANCZOS)
        sheet.paste(frame, ((index % 5) * 216, (index // 5) * 384))
    sheet.save(REVIEW_DIR / "odin-v-pole-voin-frames.jpg", quality=95, subsampling=0)
    shutil.copy2(FRAME_DIR / "odin-voin-01.jpg", REVIEW_DIR / "odin-v-pole-voin-poster.jpg")

    # Eight storefront angles, matching the other products.  Frames 01/03/05/07
    # become front / side / back / other side in the five-photo gallery.
    spin_map = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8, 8: 10}
    SPIN.mkdir(parents=True, exist_ok=True)
    for target_frame, source_frame in spin_map.items():
        shutil.copy2(
            FRAME_DIR / f"odin-voin-{source_frame:02d}.jpg",
            SPIN / f"odin-voin-{target_frame:02d}-v3.jpg",
        )


def build_white_review_video() -> None:
    """Ten-second montage from corrected stills; not a new live recording."""
    command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y"]
    for frame in range(1, 11):
        command += ["-i", str(FRAME_DIR / f"odin-voin-{frame:02d}.jpg")]
    command += ["-i", str(SOURCE_VIDEO)]
    filters = []
    labels = []
    for index in range(10):
        label_name = f"v{index}"
        # Alternate a subtle push-in and pull-back, then concatenate.
        zoom = "min(zoom+0.0015,1.045)" if index % 2 == 0 else "max(1.045-0.0015*on,1.0)"
        filters.append(
            f"[{index}:v]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=30:s=720x1280:fps=30,setsar=1,fade=t=in:st=0:d=0.08,"
            f"fade=t=out:st=0.92:d=0.08[{label_name}]"
        )
        labels.append(f"[{label_name}]")
    filters.append("".join(labels) + "concat=n=10:v=1:a=0[video]")
    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[video]", "-map", "10:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", "10", "-movflags", "+faststart", str(REVIEW_VIDEO),
    ]
    subprocess.run(command, check=True)


def main() -> None:
    temporary = TARGET.with_suffix(".working.jpg")
    replace_logo(SOURCE, temporary, (270, 165, 100, 185), variant="dark", old="black")
    replace_logo(temporary, TARGET, (60, 735, 120, 240), variant="dark", old="black")
    replace_logo(TARGET, temporary, (438, 588, 32, 48), variant="light", old="white")
    replace_logo(temporary, TARGET, (675, 805, 65, 120), variant="light", old="shape")
    temporary.unlink(missing_ok=True)

    image = Image.open(TARGET).convert("RGBA")
    image = cover_back_print(image)
    image = replace_text(image, (865, 780, 220, 180), old_box=(850, 760, 260, 220))
    image = update_labels_and_footer(image)
    image.convert("RGB").save(TARGET, quality=96, subsampling=0)

    # Separate clean views are useful in the approval sheet and video review.
    image.crop((0, 0, 700, 700)).convert("RGB").save(DROP / "odin-voin-front-v3.jpg", quality=96, subsampling=0)
    image.crop((700, 0, 1402, 700)).convert("RGB").save(DROP / "odin-voin-back-v3.jpg", quality=96, subsampling=0)

    # Four close-ups in the same 1080×720 detail format as the other tees.
    detail = Image.new("RGB", (1080, 720), (222, 222, 218))
    detail_cells = [
        (0, 700, 286, 1013),
        (286, 700, 558, 1013),
        (558, 700, 843, 1013),
        (843, 700, 1128, 1013),
    ]
    for index, bounds in enumerate(detail_cells):
        crop = image.crop(bounds).convert("RGB")
        fitted = ImageOps.contain(crop, (536, 356), Image.Resampling.LANCZOS)
        cell = ImageOps.fit(crop, (540, 360), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(12))
        cell.paste(fitted, ((540 - fitted.width) // 2, (360 - fitted.height) // 2))
        detail.paste(cell, ((index % 2) * 540, (index // 2) * 360))
    detail.save(DROP / "odin-voin-detail-v3.jpg", quality=96, subsampling=0)

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TARGET, REVIEW_DIR / "odin-v-pole-voin-board.jpg")
    build_white_review_frames()
    build_white_review_video()


if __name__ == "__main__":
    main()
