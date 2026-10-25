#!/usr/bin/env python3
"""Build the Sila mini-app hero from the authentic founder photograph.

The edit is deterministic, non-generative, and preserves the face, shirt and
print from the source: crop/reframe, subtle background treatment, and tonal
adjustments only. It never redraws or invents facial or garment detail:
no face or garment pixels are generated.
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "miniapp/assets/drop/tee-real-8272.jpg"
OUTPUT = ROOT / "miniapp/assets/drop/hero-sila-miniapp-v3.jpg"
BACKGROUND_OUTPUT = ROOT / "miniapp/assets/drop/hero-sila-layer-bg-v3.jpg"
SUBJECT_OUTPUT = ROOT / "miniapp/assets/drop/hero-sila-layer-subject-v3.webp"
# The same authentic concrete plate that backs the white tee's 360° media
# (see tools/build_white_tee_concrete_media.py). Reused as-is: the founder's
# face/shirt pixels stay untouched, only the backdrop behind him changes.
CONCRETE_PLATE = ROOT / "miniapp/assets/product-proof/white-tee-concrete-bg-v1.png"

# The mini-app mobile hero frame is 4:4.35. This crop keeps the complete head
# and chest print while removing some empty gym background around the model.
CROP = (60, 0, 820, 826)  # 760x826
OUTPUT_SIZE = (900, 978)

# A deliberately generous silhouette polygon protects all recognizable person
# and shirt pixels from the background treatment. Feathering hides the mask edge.
SUBJECT_POLYGON = [
    (150, 0), (400, 0), (450, 48), (480, 125), (492, 220), (478, 300),
    (445, 360), (510, 378), (610, 395), (690, 425), (742, 480),
    (758, 595), (760, 826), (0, 826), (0, 780), (18, 710), (40, 630),
    (52, 545), (62, 485), (92, 440), (125, 414), (168, 394), (160, 345),
    (136, 265), (132, 170), (137, 85),
]


def build_subject_mask(size: tuple[int, int], feather: float = 18) -> Image.Image:
    mask = Image.new("L", (760, 826), 0)
    ImageDraw.Draw(mask).polygon(SUBJECT_POLYGON, fill=255)
    return mask.resize(size, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(feather))


# Tighter silhouette in OUTPUT_SIZE coordinates, traced on a coordinate grid
# over the finished crop. Used only for the layered "living poster": the same
# authentic pixels, split into background / typography / subject planes.
CUTOUT_POLYGON = [
    # Head contour measured row-by-row from luminance gradients of the source
    # crop (+4px safety): the earlier loose contour dragged bright wall pixels
    # into the subject layer, which glowed against the dark background plane.
    (235, 62), (325, 54), (415, 62), (442, 84), (452, 104), (456, 134),
    (460, 164), (493, 204), (522, 244), (522, 284), (503, 324), (508, 364),
    (518, 404), (549, 444), (575, 465), (650, 480), (760, 510),
    (850, 555), (900, 610), (900, 978), (0, 978), (8, 900), (35, 820),
    (60, 720), (88, 630), (125, 565), (190, 535), (285, 520), (305, 478),
    (245, 452), (228, 444), (220, 404), (213, 364), (198, 324), (188, 284),
    (190, 244), (181, 204), (184, 164), (188, 134), (192, 106), (206, 84),
]


def build_cutout_mask(size: tuple[int, int]) -> Image.Image:
    mask = Image.new("L", OUTPUT_SIZE, 0)
    ImageDraw.Draw(mask).polygon(CUTOUT_POLYGON, fill=255)
    if size != OUTPUT_SIZE:
        mask = mask.resize(size, Image.Resampling.LANCZOS)
    # Slight erosion + short feather: no bright halo against the typography.
    return mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(2.5))


def dilate_mask(mask: Image.Image, radius: int = 45, feather: float = 18) -> Image.Image:
    return mask.filter(ImageFilter.MaxFilter(radius)).filter(ImageFilter.GaussianBlur(feather))


def add_subtle_burgundy_background(image: Image.Image, subject: Image.Image) -> Image.Image:
    width, height = image.size
    # Restrained red is confined to the out-of-focus background, never the face,
    # shirt, or lettering. Strongest beside the head/shoulders and fading down,
    # it reads as a subtle edge light in the brand palette, not as a filter.
    horizontal = Image.new("L", (width, 1), 0)
    draw = ImageDraw.Draw(horizontal)
    for x in range(width):
        edge = min(x / 210, (width - 1 - x) / 240, 1.0)
        draw.point((x, 0), fill=int(255 * max(0.0, edge)))
    horizontal = horizontal.resize((width, height))
    vertical = Image.new("L", (1, height), 0)
    draw = ImageDraw.Draw(vertical)
    for y in range(height):
        draw.point((0, y), fill=int(255 * (1.0 - 0.55 * y / max(1, height - 1))))
    vertical = vertical.resize((width, height))
    strength = ImageChops.multiply(horizontal, vertical).point(lambda v: v * 30 // 255)
    background = ImageOps.invert(subject)
    alpha = ImageChops.multiply(strength, background)
    overlay = Image.new("RGB", (width, height), (96, 10, 22))
    return Image.composite(overlay, image, alpha)


def apply_vignette(image: Image.Image, strength: float = 0.5) -> Image.Image:
    """Darken frame edges toward the page black, keeping a soft pool of light
    behind the head/shoulders. Applied to background planes only."""
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (-width * 0.25, -height * 0.32, width * 1.25, height * 1.05), fill=255
    )
    mask = mask.filter(ImageFilter.GaussianBlur(140))
    multiplier = mask.point(lambda v: int(255 - (255 - v) * strength))
    return ImageChops.multiply(image, multiplier.convert("RGB"))


def concrete_background(cutout: Image.Image) -> Image.Image:
    """Brand concrete plate as the hero backdrop — the same wall the white
    tee is shown against. Cover-cropped (anchored to the light pool at the
    top), gently darkened, vignetted into the page black, with a soft
    contact shadow inside the dilated silhouette so the figure sits on the
    wall instead of floating over it."""
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
    background = plate.crop((left, top, left + width, top + height))
    background = ImageEnhance.Brightness(background).enhance(0.9)
    background = ImageEnhance.Contrast(background).enhance(1.02)
    shadowed = ImageEnhance.Brightness(background).enhance(0.72)
    background = Image.composite(shadowed, background, dilate_mask(cutout, radius=31, feather=30))
    background = apply_vignette(background, strength=0.5)
    return background


def dark_studio_background(crop: Image.Image, blur: float, brightness: float) -> Image.Image:
    """Melt the room into a dark, desaturated haze: at desktop scale the old
    light blur still read as a messy interior. No new detail is invented —
    the real background is only defocused and darkened away."""
    background = crop.filter(ImageFilter.GaussianBlur(blur))
    background = ImageEnhance.Brightness(background).enhance(brightness)
    background = ImageEnhance.Color(background).enhance(0.52)
    return background


def main() -> None:
    with Image.open(SOURCE) as source:
        original = source.convert("RGB")
        if original.width != 900 or original.height < 826:
            raise ValueError(f"Unexpected source dimensions: {original.size}")

        crop = original.crop(CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
        subject = build_subject_mask(OUTPUT_SIZE)

        # Quiet the gym background while keeping the person crisp and unchanged.
        softened = dark_studio_background(crop, blur=44, brightness=0.27)
        softened = apply_vignette(softened, strength=0.62)
        image = Image.composite(crop, softened, subject)
        image = add_subtle_burgundy_background(image, subject)

        # Gentle neutral grade. Lift only dark midtones so shirt texture reads;
        # bright face and white chest lettering are essentially unaffected.
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
        image = Image.composite(lifted, image, lift_mask)

        # --- Living-poster layers: identical pixels, split into planes. ---
        # The flat OUTPUT is saved after the planes: it is the flattened poster
        # (dark background plane + subject), so the fallback hero and the film
        # opening frame show exactly the same treated background as the poster.
        cutout = build_cutout_mask(OUTPUT_SIZE)
        # Edge decontamination: the hand-traced contour keeps a rim of the
        # original bright wall around the head. Against the concrete plane
        # that rim would glow like a halo, so shade only the outer band of
        # the silhouette; face and print pixels inside are untouched.
        inner = cutout.filter(ImageFilter.MinFilter(21)).filter(ImageFilter.GaussianBlur(6))
        band = ImageChops.subtract(cutout, inner).filter(ImageFilter.GaussianBlur(4))
        shaded = ImageEnhance.Color(ImageEnhance.Brightness(image).enhance(0.45)).enhance(0.6)
        cleaned = Image.composite(shaded, image, band)
        subject_layer = cleaned.convert("RGBA")
        subject_layer.putalpha(cutout)
        subject_layer.save(SUBJECT_OUTPUT, "WEBP", quality=90, method=6, alpha_quality=100)

        # Background plane: the brand concrete wall (same plate as the white
        # tee's 360° media) with a baked contact shadow and vignette, so the
        # typography can sit between the wall and the figure.
        background = concrete_background(cutout)
        background.save(BACKGROUND_OUTPUT, "JPEG", quality=92, optimize=True, progressive=True)

        flattened = background.convert("RGBA")
        flattened.alpha_composite(subject_layer)
        flattened.convert("RGB").save(OUTPUT, "JPEG", quality=94, optimize=True, progressive=True)
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]})")
    print(f"Wrote {BACKGROUND_OUTPUT.relative_to(ROOT)} + {SUBJECT_OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
