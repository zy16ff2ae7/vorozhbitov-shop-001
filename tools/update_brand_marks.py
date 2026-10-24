#!/usr/bin/env python3
"""Create versioned UI seals/avatars from the approved-shape sword monogram."""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
LOGO = ASSETS / "brand/sword-monogram-light.png"
FONT = ROOT / "miniapp/fonts/sila-chest-400.woff2"


def seal(size: int) -> Image.Image:
    canvas = Image.new("RGB", (size, size), (3, 3, 3))
    draw = ImageDraw.Draw(canvas)
    margin = max(3, round(size * 0.035))
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), outline=(184, 184, 180), width=max(1, round(size * 0.006)))
    logo = Image.open(LOGO).convert("RGBA")
    logo.thumbnail((round(size * 0.66), round(size * 0.82)), Image.Resampling.LANCZOS)
    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    return canvas.convert("RGB")


def emblem(size: int = 1254) -> Image.Image:
    rng = np.random.default_rng(1601)
    coarse = rng.normal(0, 1, (28, 28)).astype(np.float32)
    medium = rng.normal(0, 1, (110, 110)).astype(np.float32)
    coarse = cv2.resize(coarse, (size, size), interpolation=cv2.INTER_CUBIC)
    medium = cv2.resize(medium, (size, size), interpolation=cv2.INTER_CUBIC)
    texture = np.clip(12 + coarse * 7 + medium * 3, 3, 36).astype(np.uint8)
    pixels = np.stack([texture, texture, texture], axis=2)
    yy, xx = np.mgrid[0:size, 0:size]
    radius = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    pixels[radius > size * 0.485] = 2
    canvas = Image.fromarray(pixels, "RGB").convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((18, 18, size - 19, size - 19), outline=(170, 170, 166, 255), width=4)
    logo = Image.open(LOGO).convert("RGBA")
    logo.thumbnail((570, 850), Image.Resampling.LANCZOS)
    canvas.alpha_composite(logo, ((size - logo.width) // 2, 85))
    text = "СИЛА И ЧЕСТЬ"
    font = ImageFont.truetype(str(FONT), 69)
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text(((size - (bounds[2] - bounds[0])) // 2, 1000), text, font=font, fill=(218, 216, 210, 255))
    draw.line((310, 1105, 565, 1105), fill=(170, 170, 166, 255), width=2)
    draw.line((689, 1105, 944, 1105), fill=(170, 170, 166, 255), width=2)
    return canvas.convert("RGB")


def main() -> None:
    outputs = {
        "sila-chest-seal-v2.jpg": seal(512),
        "bot-avatar-v2.jpg": seal(256),
        "bot-mark-v2.jpg": seal(128),
        "sila-chest-emblem-v2.jpg": emblem(),
    }
    for name, image in outputs.items():
        image.save(ASSETS / name, quality=96, subsampling=0)


if __name__ == "__main__":
    main()
