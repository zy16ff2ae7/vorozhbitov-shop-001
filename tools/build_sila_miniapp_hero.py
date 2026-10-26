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

CROP = (60, 0, 820, 826)
OUTPUT_SIZE = (900, 978)

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


CUTOUT_POLYGON = [
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
    return mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(2.5))


def dilate_mask(mask: Image.Image, radius: int = 45, feather: float = 18) -> Image.Image:
    return mask.filter(ImageFilter.MaxFilter(radius)).filter(ImageFilter.GaussianBlur(feather))


def add_subtle_burgundy_background(image: Image.Image, subject: Image.Image) -> Image.Image:
    width, height = image.size
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
    overlay = Image.new("RGB", (width, height), (192, 34, 23))
    return Image.composite(overlay, image, alpha)


def apply_vignette(image: Image.Image, strength: float = 0.55) -> Image.Image:
    width, height = image.size
    vignette = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(vignette)
    cx, cy = width / 2, height * 0.42
    rx, ry = width * 0.58, height * 0.62
    max_steps = 48
    for i in range(max_steps):
        t = i / max_steps
        cur_rx = rx * (1.0 + 0.35 * (1.0 - t))
        cur_ry = ry * (1.0 + 0.35 * (1.0 - t))
        box = [cx - cur_rx * (1 - t * 0.6), cy - cur_ry * (1 - t * 0.6),
               cx + cur_rx * (1 - t * 0.6), cy + cur_ry * (1 - t * 0.6)]
        shade = int(255 * (1.0 - strength * (t ** 1.6)))
        draw.ellipse(box, fill=shade)
    vignette = vignette.filter(ImageFilter.GaussianBlur(36))
    black = Image.new("RGB", (width, height), (7, 7, 8))
    return Image.composite(image, black, vignette)


def concrete_background(cutout: Image.Image) -> Image.Image:
    width, height = OUTPUT_SIZE
    plate = Image.open(CONCRETE_PLATE).convert("RGB")
    scale = max(width / plate.width, height / plate.height) * 1.05
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
    background = crop.filter(ImageFilter.GaussianBlur(blur))
    background = ImageEnhance.Brightness(background).enhance(brightness)
    background = ImageEnhance.Color(background).enhance(0.52)
    return background


def retouch_face_commercial(image_pil: Image.Image) -> Image.Image:
    """Editorial portrait retouching: clean temporary blemishes, balance skin tones, preserve 100% pores & identity."""
    import cv2
    import numpy as np

    arr_rgb = np.array(image_pil)
    bgr = cv2.cvtColor(arr_rgb, cv2.COLOR_RGB2BGR)
    h, w, _ = bgr.shape

    # 1. Inpaint forehead & chin blemishes
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (348, 105), 6, 255, -1)
    cv2.circle(mask, (332, 360), 6, 255, -1)
    cleaned = cv2.inpaint(bgr, mask, 5, cv2.INPAINT_TELEA)

    # 2. Skin tone frequency separation (keep texture, even out blotchiness)
    face_mask = np.zeros((h, w), dtype=np.float32)
    cv2.ellipse(face_mask, (350, 230), (130, 180), 0, 0, 360, 1.0, -1)
    face_mask = cv2.GaussianBlur(face_mask, (41, 41), 15)

    smooth_base = cv2.bilateralFilter(cleaned, d=7, sigmaColor=28, sigmaSpace=12)
    blended = (cleaned.astype(np.float32) * 0.72 + smooth_base.astype(np.float32) * 0.28)
    blended = np.clip(blended, 0, 255).astype(np.uint8)

    out = cleaned.copy()
    for c in range(3):
        out[:, :, c] = (cleaned[:, :, c] * (1.0 - face_mask * 0.8) + blended[:, :, c] * (face_mask * 0.8)).astype(np.uint8)

    # 3. Eye enhancement: clarity in steel-blue irises
    eye_mask = np.zeros((h, w), dtype=np.float32)
    cv2.ellipse(eye_mask, (280, 200), (22, 14), 5, 0, 360, 1.0, -1)
    cv2.ellipse(eye_mask, (415, 190), (22, 14), -5, 0, 360, 1.0, -1)
    eye_mask = cv2.GaussianBlur(eye_mask, (11, 11), 3)

    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l, a_ch, b_ch = cv2.split(lab)
    l_float = l.astype(np.float32)
    l_enhanced = np.clip((l_float - 128) * 1.15 + 132, 0, 255)
    l_final = (l_float * (1.0 - eye_mask * 0.5) + l_enhanced * (eye_mask * 0.5)).astype(np.uint8)
    lab = cv2.merge([l_final, a_ch, b_ch])
    retouched_bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    retouched_rgb = cv2.cvtColor(retouched_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(retouched_rgb)


def extract_neural_subject(source_image: Image.Image) -> Image.Image:
    """Extract human subject using neural matting (u2net_human_seg) with anatomical refinement."""
    import cv2
    import numpy as np
    import rembg

    retouched_source = retouch_face_commercial(source_image)

    session = rembg.new_session("u2net_human_seg")
    cutout = rembg.remove(retouched_source, session=session, decontaminate=True)
    r, g, b, a = cutout.split()
    alpha = np.array(a)

    # Clean binary threshold to remove distant background haze
    binary = (alpha > 120).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    eroded = cv2.erode(binary, kernel, iterations=1)

    # Anatomical neck contour refinement (clean gym wall behind neck)
    for y in range(240, 360):
        x_edge = int(495 + (y - 240) * (511 - 495) / (360 - 240))
        eroded[y, x_edge + 1:] = 0

    # Natural collar and shoulder slope
    for x in range(511, 620):
        t = (x - 511) / (620 - 511)
        y_collar = int(360 + 25 * (t ** 0.8))
        eroded[:y_collar, x] = 0

    alpha_clean = cv2.GaussianBlur(eroded, (3, 3), 0.6)

    # Crop to 900x978 hero frame
    subject_frame = Image.merge("RGBA", (r, g, b, Image.fromarray(alpha_clean))).crop((0, 0, 900, 978))
    sub_arr = np.array(subject_frame)

    # Soft natural fade at bottom edge (y: 920..978)
    for y in range(920, 978):
        fade = 1.0 - (y - 920) / (978 - 920)
        sub_arr[y, :, 3] = (sub_arr[y, :, 3] * fade).astype(np.uint8)

    return Image.fromarray(sub_arr)


def main() -> None:
    with Image.open(SOURCE) as source:
        original = source.convert("RGB")
        if original.width != 900 or original.height < 826:
            raise ValueError(f"Unexpected source dimensions: {original.size}")

        try:
            subject_layer = extract_neural_subject(source)
            cutout_mask = subject_layer.split()[3]
        except Exception as err:
            print(f"Neural extraction fallback: {err}")
            crop = original.crop(CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
            cutout_mask = build_cutout_mask(OUTPUT_SIZE)
            subject_layer = crop.convert("RGBA")
            subject_layer.putalpha(cutout_mask)

        # Save transparent subject layer
        subject_layer.save(SUBJECT_OUTPUT, "WEBP", quality=95, method=6, alpha_quality=100)

        # Background plane: brand concrete wall with contact shadow
        background = concrete_background(cutout_mask)
        background.save(BACKGROUND_OUTPUT, "JPEG", quality=92, optimize=True, progressive=True)

        # Flat composite fallback
        shadow_mask = cutout_mask.filter(ImageFilter.GaussianBlur(15))
        bg_composite = background.copy().convert("RGBA")
        shadow = Image.new("RGBA", OUTPUT_SIZE, (0, 0, 0, 140))
        bg_composite.paste(shadow, (0, 4), mask=shadow_mask)

        flattened = Image.alpha_composite(bg_composite, subject_layer).convert("RGB")
        flattened.save(OUTPUT, "JPEG", quality=94, optimize=True, progressive=True)

    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]})")
    print(f"Wrote {BACKGROUND_OUTPUT.relative_to(ROOT)} + {SUBJECT_OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
