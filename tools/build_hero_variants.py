#!/usr/bin/env python3
"""Three candidate treatments for the founder hero (v4 exploration).

All variants are non-generative: face, shirt and print pixels come from the
same graded crop as the shipped builder. They differ only in how the
silhouette edge and the backdrop are treated:

A "clean concrete" — brand concrete plate, NO dark band on the silhouette
  edge and NO outline shadow; one soft shadow dropped down-left only.
B "dark studio" — the real room melted into a dark haze; the hairline
  silhouette dissolves into the backdrop, so there is no visible cut edge.
C "honest frame" — no cutout at all: full original crop, gentle grade and
  vignette. Zero edge artifacts by construction.

Outputs preview JPGs + a labelled contact sheet into /tmp/hero-variants.
"""
from pathlib import Path
import sys

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_sila_miniapp_hero import (  # noqa: E402
    CONCRETE_PLATE, CROP, OUTPUT_SIZE, SOURCE,
    add_subtle_burgundy_background, apply_vignette, build_cutout_mask,
    build_subject_mask, dark_studio_background, dilate_mask,
)

OUT = Path("/tmp/hero-variants")
OUT.mkdir(parents=True, exist_ok=True)


def graded_crop() -> Image.Image:
    """The same subject treatment as the shipped builder (no layer split)."""
    with Image.open(SOURCE) as source:
        original = source.convert("RGB")
    crop = original.crop(CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
    subject = build_subject_mask(OUTPUT_SIZE)
    softened = dark_studio_background(crop, blur=44, brightness=0.27)
    softened = apply_vignette(softened, strength=0.62)
    image = Image.composite(crop, softened, subject)
    image = add_subtle_burgundy_background(image, subject)
    image = ImageEnhance.Color(image).enhance(0.96)
    image = ImageEnhance.Contrast(image).enhance(1.035)
    luminance = ImageOps.grayscale(image)
    dark_mask = luminance.point(lambda value: max(0, min(255, (138 - value) * 2)))
    shirt_region = Image.new("L", (760, 826), 0)
    ImageDraw.Draw(shirt_region).polygon(
        [(170, 390), (280, 350), (390, 355), (510, 375), (620, 395),
         (710, 435), (760, 490), (760, 826), (20, 826), (42, 735),
         (70, 625), (90, 525), (115, 465)], fill=255
    )
    shirt_region = shirt_region.resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
    lift_mask = ImageChops.multiply(dark_mask, shirt_region).filter(ImageFilter.GaussianBlur(6))
    lifted = ImageEnhance.Brightness(image).enhance(1.11)
    return Image.composite(lifted, image, lift_mask)


def subject_layer(image: Image.Image, band_strength: float | None) -> Image.Image:
    cutout = build_cutout_mask(OUTPUT_SIZE)
    cleaned = image
    if band_strength is not None:
        inner = cutout.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.GaussianBlur(4))
        band = ImageChops.subtract(cutout, inner).filter(ImageFilter.GaussianBlur(3))
        shaded = ImageEnhance.Brightness(image).enhance(band_strength)
        cleaned = Image.composite(shaded, image, band)
    layer = cleaned.convert("RGBA")
    layer.putalpha(cutout)
    return layer


def concrete_plate() -> Image.Image:
    with Image.open(CONCRETE_PLATE) as plate_file:
        plate = plate_file.convert("RGB")
    width, height = OUTPUT_SIZE
    scale = max(width / plate.width, height / plate.height)
    plate = plate.resize(
        (round(plate.width * scale), round(plate.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = (plate.width - width) // 2
    top = min(20, plate.height - height)
    return plate.crop((left, top, left + width, top + height))


def variant_a(image: Image.Image) -> Image.Image:
    background = concrete_plate()
    background = ImageEnhance.Brightness(background).enhance(0.84)
    background = ImageEnhance.Contrast(background).enhance(1.02)
    cutout = build_cutout_mask(OUTPUT_SIZE)
    # One soft cast shadow down-left of the figure (light comes from the
    # upper right in the source): grounds him without outlining the head.
    shadow_mask = dilate_mask(cutout, radius=25, feather=42)
    shadow_mask = ImageChops.offset(shadow_mask, -14, 30)
    shadow_mask = ImageChops.subtract(shadow_mask, cutout)
    shadowed = ImageEnhance.Brightness(background).enhance(0.66)
    background = Image.composite(shadowed, background, shadow_mask)
    background = apply_vignette(background, strength=0.38)
    flat = background.convert("RGBA")
    # Only a whisper of edge shading (0.82, narrow) instead of the dark 0.45
    # band that sat on the crown like a helmet.
    flat.alpha_composite(subject_layer(image, band_strength=0.82))
    return flat.convert("RGB")


def variant_b(image: Image.Image) -> Image.Image:
    with Image.open(SOURCE) as source:
        original = source.convert("RGB")
    crop = original.crop(CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
    background = dark_studio_background(crop, blur=44, brightness=0.2)
    background = apply_vignette(background, strength=0.6)
    flat = background.convert("RGBA")
    flat.alpha_composite(subject_layer(image, band_strength=None))
    return flat.convert("RGB")


def variant_c() -> Image.Image:
    with Image.open(SOURCE) as source:
        original = source.convert("RGB")
    crop = original.crop(CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
    image = ImageEnhance.Color(crop).enhance(0.94)
    image = ImageEnhance.Contrast(image).enhance(1.04)
    image = ImageEnhance.Brightness(image).enhance(0.97)
    return apply_vignette(image, strength=0.42)


def contact_sheet(paths: list[Path]) -> Path:
    thumb_w = 420
    thumbs = []
    for path in paths:
        with Image.open(path) as img:
            thumbs.append(img.convert("RGB").resize(
                (thumb_w, round(thumb_w * img.height / img.width)), Image.Resampling.LANCZOS))
    label_h = 44
    sheet = Image.new("RGB", (thumb_w * len(thumbs), thumbs[0].height + label_h), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    labels = ["A  CLEAN CONCRETE", "B  DARK STUDIO", "C  HONEST FRAME"]
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, (i * thumb_w, label_h))
        draw.text((i * thumb_w + 14, 13), labels[i], fill=(240, 235, 225))
    out = OUT / "hero-variants-sheet.jpg"
    sheet.save(out, "JPEG", quality=90, optimize=True)
    return out


def main() -> None:
    image = graded_crop()
    results = []
    for name, maker in (("hero-A-clean-concrete", lambda: variant_a(image)),
                        ("hero-B-dark-studio", lambda: variant_b(image)),
                        ("hero-C-honest-frame", variant_c)):
        path = OUT / f"{name}.jpg"
        maker().save(path, "JPEG", quality=94, optimize=True, progressive=True)
        results.append(path)
        print("wrote", path)
    print("wrote", contact_sheet(results))


if __name__ == "__main__":
    main()
