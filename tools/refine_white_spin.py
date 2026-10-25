#!/usr/bin/env python3
"""Build the white tee's landscape 360° set from committed source images.

The eight outputs are montage visualisations, not a new live recording.  They
share the black tees' 1080×590 concrete plate, garment height and baseline.
Existing v3–v5 assets and all customer/source images are left untouched.  No
``task_materials`` archive is required: every input is committed in miniapp/
or approval/.

Requires Pillow, NumPy and OpenCV (see requirements-media.txt).
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
REVIEW = ROOT / "approval/brand-logo-review-2026-10-25-v2"
FRAME_DIR = REVIEW / "white-frames"
SOURCE_FIRST_SIDE = REVIEW / "white-sources/odin-voin-03-original.jpg"
SOURCE_SIDE = REVIEW / "white-sources/odin-voin-08-original.jpg"
PLATE_SOURCE = MINIAPP / "assets/spin/tee-sich-03-v4.jpg"
SPIN = MINIAPP / "assets/spin"
CANVAS_SIZE = (1080, 590)
GARMENT_HEIGHT = 455
GARMENT_BASELINE = 520


def concrete_plate() -> Image.Image:
    """Restore the small central area hidden by every composited garment."""
    image = cv2.imread(str(PLATE_SOURCE))
    if image is None:
        raise FileNotFoundError(PLATE_SOURCE)
    mask = np.zeros(image.shape[:2], np.uint8)
    polygon = np.array(
        [(535, 52), (485, 65), (455, 120), (445, 535),
         (650, 535), (645, 150), (610, 75)],
        np.int32,
    )
    cv2.fillPoly(mask, [polygon], 255)
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8))
    restored = cv2.inpaint(image, mask, 9, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(restored, cv2.COLOR_BGR2RGB))


def clean_first_side() -> Image.Image:
    """Remove clipped back-print strokes without changing the side outline."""
    image = cv2.imread(str(SOURCE_FIRST_SIDE))
    if image is None:
        raise FileNotFoundError(SOURCE_FIRST_SIDE)

    # Reuse clean cloth from lower on the same panel.  Row-wise compensation
    # matches its brightness to the target; the left feather hides the join.
    # The original garment silhouette clips the patch, so it cannot grow into
    # the black background around the partially visible back print.
    x, y, width, height, offset = 380, 420, 140, 320, 340
    source = image[y + offset:y + offset + height, x:x + width].astype(np.float32)
    target = image[y:y + height, x:x + width].astype(np.float32)
    top = (image[y - 1, x:x + width].astype(np.float32) - source[0])[None, :, :]
    bottom = (image[y + height, x:x + width].astype(np.float32) - source[-1])[None, :, :]
    top = cv2.GaussianBlur(top, (0, 0), 3)
    bottom = cv2.GaussianBlur(bottom, (0, 0), 3)
    amount = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    source = np.clip(source + top * (1 - amount) + bottom * amount, 0, 255)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bright = (gray > 34).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contour = max(contours, key=cv2.contourArea)
    silhouette = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(silhouette, [contour], -1, 255, -1)

    xx = np.arange(width, dtype=np.float32)[None, :]
    yy = np.arange(height, dtype=np.float32)[:, None]
    weight = np.minimum(1, yy / 8)
    weight = weight * np.minimum(1, (height - 1 - yy) / 8)
    weight = weight * np.minimum(1, xx / 24)
    weight = weight * (silhouette[y:y + height, x:x + width] > 0)
    weight = np.clip(weight, 0, 1)[..., None]
    image[y:y + height, x:x + width] = (
        source * weight + target * (1 - weight)
    ).astype(np.uint8)

    # The source's generated side seam becomes a dark zigzag after resizing.
    # Replace only that interior strip with adjacent cloth.  Boundary-wise
    # brightness correction and a twelve-pixel feather retain the natural
    # folds around it without leaving a rectangular patch.
    x, y, width, height = 285, 735, 60, 355
    source = image[y:y + height, x - 55:x - 55 + width].astype(np.float32)
    target = image[y:y + height, x:x + width].astype(np.float32)
    left = (image[y:y + height, x - 1].astype(np.float32) - source[:, 0])[:, None, :]
    right = (image[y:y + height, x + width].astype(np.float32) - source[:, -1])[:, None, :]
    left = cv2.GaussianBlur(left, (0, 0), 3)
    right = cv2.GaussianBlur(right, (0, 0), 3)
    amount = np.linspace(0, 1, width, dtype=np.float32)[None, :, None]
    source = np.clip(source + left * (1 - amount) + right * amount, 0, 255)
    xx = np.arange(width, dtype=np.float32)[None, :]
    yy = np.arange(height, dtype=np.float32)[:, None]
    weight = np.minimum(1, yy / 12) * np.minimum(1, (height - 1 - yy) / 12)
    weight = weight * np.minimum(1, xx / 12) * np.minimum(1, (width - 1 - xx) / 12)
    weight = np.clip(weight, 0, 1)[..., None]
    image[y:y + height, x:x + width] = (
        source * weight + target * (1 - weight)
    ).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def clean_other_side() -> Image.Image:
    """Replace all clipped back print, including strokes at the outer edge."""
    image = cv2.imread(str(SOURCE_SIDE))
    if image is None:
        raise FileNotFoundError(SOURCE_SIDE)
    x, y, width, height, offset = 180, 400, 110, 360, 340
    source = image[y + offset:y + offset + height, x:x + width].astype(np.float32)
    target = image[y:y + height, x:x + width].astype(np.float32)
    top = (image[y - 1, x:x + width].astype(np.float32) - source[0])[None, :, :]
    bottom = (image[y + height, x:x + width].astype(np.float32) - source[-1])[None, :, :]
    top = cv2.GaussianBlur(top, (0, 0), 3)
    bottom = cv2.GaussianBlur(bottom, (0, 0), 3)
    amount = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    source = np.clip(source + top * (1 - amount) + bottom * amount, 0, 255)

    xx = np.arange(width, dtype=np.float32)[None, :]
    yy = np.arange(height, dtype=np.float32)[:, None]
    weight = np.minimum(1, yy / 8)
    weight = weight * np.minimum(1, (height - 1 - yy) / 8)
    weight = weight * np.minimum(1, (width - 1 - xx) / 24)
    weight = np.clip(weight, 0, 1)[..., None]
    image[y:y + height, x:x + width] = (
        source * weight + target * (1 - weight)
    ).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def garment_cutout(
    source: Path | Image.Image,
    *,
    allowed_polygon: list[tuple[int, int]] | None = None,
    include_polygons: list[list[tuple[int, int]]] | None = None,
    exclude_polygons: list[list[tuple[int, int]]] | None = None,
) -> Image.Image:
    image = source.copy() if isinstance(source, Image.Image) else Image.open(source)
    pixels = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    bright = (gray > 34).astype(np.uint8) * 255
    if allowed_polygon:
        allowed = np.zeros(gray.shape, np.uint8)
        cv2.fillPoly(allowed, [np.array(allowed_polygon, np.int32)], 255)
        bright = cv2.bitwise_and(bright, allowed)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("Garment contour was not found")
    contour = max(contours, key=cv2.contourArea)
    mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))

    # Detached letters, logo fragments and hanging editing artefacts are not
    # part of the garment.  Real prints/labels inside the contour are retained.
    count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8))
    if count > 1:
        keep = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        mask = np.where(labels == keep, 255, 0).astype(np.uint8)

    # Dark hanging labels cannot be recovered from a white-on-black threshold
    # alone.  Include the real label's complete rectangle explicitly, and cut
    # away the duplicated label generated in the source side frame.
    for polygon in exclude_polygons or []:
        cv2.fillPoly(mask, [np.array(polygon, np.int32)], 0)
    for polygon in include_polygons or []:
        cv2.fillPoly(mask, [np.array(polygon, np.int32)], 255)

    mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
    mask = cv2.GaussianBlur(mask, (0, 0), 0.75)

    bounds = cv2.findNonZero((mask > 127).astype(np.uint8))
    if bounds is None:
        raise RuntimeError("Garment mask is empty")
    x, y, width, height = cv2.boundingRect(bounds)
    pad = 3
    x, y = max(0, x - pad), max(0, y - pad)
    width = min(image.width - x, width + 2 * pad)
    height = min(image.height - y, height + 2 * pad)

    # The garment, print and labels are monochrome.  Neutralising the cutout
    # removes JPEG colour fringes from the source without changing its ink or
    # fabric luminance; the concrete plate keeps its own colour and texture.
    neutral = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    pixels = cv2.cvtColor(neutral, cv2.COLOR_GRAY2RGB)
    rgba = Image.fromarray(pixels).convert("RGBA")
    rgba.putalpha(Image.fromarray(mask))
    return rgba.crop((x, y, x + width, y + height))


def composite(
    plate: Image.Image,
    cutout: Image.Image,
    *,
    width_scale: float = 1.0,
    garment_height: int = GARMENT_HEIGHT,
    center_x: float = CANVAS_SIZE[0] / 2,
) -> Image.Image:
    width = round(cutout.width * garment_height / cutout.height * width_scale)
    cutout = cutout.resize((width, garment_height), Image.Resampling.LANCZOS)
    x = round(center_x - cutout.width / 2)
    y = GARMENT_BASELINE - cutout.height
    canvas = plate.copy().convert("RGBA")

    alpha = cutout.getchannel("A")
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_shape = Image.new("RGBA", cutout.size, (0, 0, 0, 70))
    shadow_shape.putalpha(alpha.point(lambda value: value * 40 // 100))
    shadow.alpha_composite(shadow_shape, (x, y + 7))
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(cutout, (x, y))
    return canvas.convert("RGB")


def build_white_spin() -> list[Path]:
    plate = concrete_plate()
    first_side = clean_first_side()
    other_side = clean_other_side()
    front_tag = [[(499, 1048), (533, 1050), (530, 1116), (497, 1112)]]
    angle_tag = [[(327, 1054), (378, 1057), (369, 1117), (316, 1108)]]
    first_side_tag = [[(178, 1038), (208, 1048), (202, 1103), (169, 1094)]]
    duplicate_side_tag = [[
        (140, 1052), (164, 1058), (178, 1065),
        (169, 1098), (140, 1095),
    ]]
    other_side_print_edge = [[
        (0, 380), (225, 380), (214, 450), (208, 550),
        (204, 650), (203, 780), (0, 780),
    ]]
    other_side_clean_panel = [[
        (225, 380), (310, 380), (310, 780), (203, 780),
        (204, 650), (208, 550), (214, 450),
    ]]
    # Frames 01/05 are the same free-floating front/back views used for the
    # black tees.  Their sleeve tips touch the portrait source boundary; this
    # shared symmetric outline trims those tips to complete, natural curves.
    # It does not alter the collar, torso, prints or straight garment hem.
    symmetric_outline = [
        (250, 260), (470, 260), (560, 310), (630, 400),
        (675, 450), (700, 500), (705, 550), (705, 600),
        (695, 650), (680, 680), (660, 705), (580, 755),
        (590, 1080), (570, 1120), (150, 1120), (130, 1080),
        (140, 755), (60, 705), (40, 680), (25, 650),
        (15, 600), (15, 550), (20, 500), (45, 450),
        (90, 400), (160, 320),
    ]
    sources = [
        (FRAME_DIR / "odin-voin-01.jpg", front_tag, None, symmetric_outline),
        (FRAME_DIR / "odin-voin-02.jpg", angle_tag, None, None),
        (first_side, first_side_tag, duplicate_side_tag, None),
        (FRAME_DIR / "odin-voin-04.jpg", None, None, None),
        (FRAME_DIR / "odin-voin-05.jpg", None, None, symmetric_outline),
        (FRAME_DIR / "odin-voin-06.jpg", None, None, None),
        (other_side, other_side_clean_panel, other_side_print_edge, None),
        (FRAME_DIR / "odin-voin-09.jpg", None, None, None),
    ]
    outputs = []
    for index, (source, include, exclude, allowed) in enumerate(sources, 1):
        cutout = garment_cutout(
            source,
            allowed_polygon=allowed,
            include_polygons=include,
            exclude_polygons=exclude,
        )
        frame = composite(
            plate,
            cutout,
            width_scale=1.14 if index in (1, 5) else 1.0,
            garment_height=452 if index in (1, 5) else GARMENT_HEIGHT,
            center_x=535 if index in (1, 5) else CANVAS_SIZE[0] / 2,
        )
        if frame.size != CANVAS_SIZE:
            raise AssertionError(frame.size)
        output = SPIN / f"odin-voin-{index:02d}-v6.jpg"
        frame.save(output, quality=95, subsampling=0, optimize=True)
        outputs.append(output)
    return outputs


def main() -> None:
    for output in build_white_spin():
        print(output.relative_to(ROOT))


if __name__ == "__main__":
    main()
