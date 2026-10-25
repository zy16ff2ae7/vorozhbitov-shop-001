#!/usr/bin/env python3
"""Build the white tee's landscape 360° set from committed source images.

The eight outputs are montage visualisations, not a new live recording.  They
share the black tees' 1080×590 concrete plate, garment height and baseline.
Existing v3 assets and all customer/source images are left untouched.  No
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
SOURCE_SIDE = REVIEW / "white-sources/odin-voin-08-original.jpg"
BOARD = MINIAPP / "assets/drop/odin-v-pole-voin-v3.jpg"
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


def clean_other_side() -> Image.Image:
    """Replace old back-print strokes with aligned clean cloth from below.

    Only an interior strip is blended.  The source has the same garment edge;
    row-wise boundary correction avoids a rectangular brightness seam.  The
    untouched source remains in white-sources/ for review and future edits.
    """
    image = cv2.imread(str(SOURCE_SIDE))
    if image is None:
        raise FileNotFoundError(SOURCE_SIDE)
    x, y, width, height, offset = 195, 420, 90, 320, 325
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
    weight = np.minimum(1, yy / 6)
    weight = weight * np.minimum(1, (height - 1 - yy) / 6)
    weight = weight * np.minimum(1, (width - 1 - xx) / 18)
    weight = np.clip(weight, 0, 1)[..., None]
    image[y:y + height, x:x + width] = (
        source * weight + target * (1 - weight)
    ).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def garment_cutout(
    source: Path | Image.Image,
    *,
    crop: tuple[int, int, int, int] | None = None,
    board: bool = False,
) -> Image.Image:
    image = source.copy() if isinstance(source, Image.Image) else Image.open(source)
    image = image.convert("RGB")
    if crop:
        image = image.crop(crop)
    pixels = np.array(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    if board:
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        bright = ((hsv[..., 1] < 70) & (hsv[..., 2] > 70)).astype(np.uint8) * 255
    else:
        bright = (gray > 34).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("Garment contour was not found")
    contour = max(contours, key=cv2.contourArea)
    mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))

    if board:
        # The board contains a model.  Its outer garment contour already drops
        # arms; darken only enclosed skin at the collar instead of revealing a
        # bright background hole.  No skin or mannequin is composited.
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        skin = (hsv[..., 0] < 28) & (hsv[..., 1] > 62) & (hsv[..., 2] > 35)
        pixels[skin & (mask > 0)] = (18, 18, 18)

        # Fill only the central concavity between the collar edges.  A convex
        # hull supplies the outer boundary; tight top/central limits prevent it
        # from touching sleeves, underarms, shoulders or the garment hem.
        bx, by, bw, bh = cv2.boundingRect(contour)
        hull_mask = np.zeros_like(mask)
        cv2.drawContours(hull_mask, [cv2.convexHull(contour)], -1, 255, -1)
        yy, xx = np.indices(mask.shape)
        collar = (
            (hull_mask > 0) & (mask == 0)
            & (yy < by + bh * 0.25)
            & (np.abs(xx - (bx + bw / 2)) < bw * 0.22)
        )
        collar_shape = np.zeros_like(mask)
        cv2.ellipse(
            collar_shape,
            (round(bx + bw / 2), round(by + bh * 0.063)),
            (round(bw * 0.14), round(bh * 0.047)),
            0, 0, 360, 255, -1,
        )
        collar &= collar_shape > 0
        pixels[collar] = (18, 18, 18)
        mask[collar] = 255

    # Detached letters, logo fragments and hanging editing artefacts are not
    # part of the garment.  Real prints/labels inside the contour are retained.
    count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8))
    if count > 1:
        keep = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        mask = np.where(labels == keep, 255, 0).astype(np.uint8)
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
    mask = cv2.GaussianBlur(mask, (0, 0), 0.75)

    x, y, width, height = cv2.boundingRect(contour)
    pad = 3
    x, y = max(0, x - pad), max(0, y - pad)
    width = min(image.width - x, width + 2 * pad)
    height = min(image.height - y, height + 2 * pad)
    rgba = Image.fromarray(pixels).convert("RGBA")
    rgba.putalpha(Image.fromarray(mask))
    return rgba.crop((x, y, x + width, y + height))


def composite(plate: Image.Image, cutout: Image.Image) -> Image.Image:
    width = round(cutout.width * GARMENT_HEIGHT / cutout.height)
    cutout = cutout.resize((width, GARMENT_HEIGHT), Image.Resampling.LANCZOS)
    x = round(CANVAS_SIZE[0] / 2 - cutout.width / 2)
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
    other_side = clean_other_side()
    sources = [
        (BOARD, (0, 0, 700, 700), True),
        (FRAME_DIR / "odin-voin-02.jpg", None, False),
        (FRAME_DIR / "odin-voin-03.jpg", None, False),
        (FRAME_DIR / "odin-voin-04.jpg", None, False),
        (BOARD, (700, 0, 1402, 700), True),
        (FRAME_DIR / "odin-voin-06.jpg", None, False),
        (other_side, None, False),
        (FRAME_DIR / "odin-voin-09.jpg", None, False),
    ]
    outputs = []
    for index, (source, crop, board) in enumerate(sources, 1):
        cutout = garment_cutout(source, crop=crop, board=board)
        frame = composite(plate, cutout)
        if frame.size != CANVAS_SIZE:
            raise AssertionError(frame.size)
        output = SPIN / f"odin-voin-{index:02d}-v4.jpg"
        frame.save(output, quality=95, subsampling=0, optimize=True)
        outputs.append(output)
    return outputs


def main() -> None:
    for output in build_white_spin():
        print(output.relative_to(ROOT))


if __name__ == "__main__":
    main()
