#!/usr/bin/env python3
"""Build the white tee's landscape 360° set from committed source images.

The eight outputs are montage visualisations, not a new live recording.  They
share the black tees' 1080×590 concrete plate, garment height and baseline.
Existing v3/v4 assets and all customer/source images are left untouched.  No
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
    crop: tuple[int, int, int, int] | None = None,
    board: str = "",
    include_polygons: list[list[tuple[int, int]]] | None = None,
    exclude_polygons: list[list[tuple[int, int]]] | None = None,
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

        # The board photographs include a model.  Keep the complete sleeves and
        # hem, but exclude tattooed arms, the wristwatch and the neck before
        # finding the garment contour.  A brightness-only contour included the
        # watch as a bright detached artefact in the previous version.
        outlines = {
            "front": [
                (235, 28), (425, 28), (510, 65), (580, 125),
                (655, 345), (650, 375), (525, 410), (507, 398),
                (507, 525), (502, 585), (485, 620), (440, 645),
                (350, 663), (260, 660), (180, 640), (130, 610),
                (122, 580), (135, 540), (145, 415), (125, 420),
                (10, 380), (5, 350), (65, 150), (125, 80),
            ],
            "back": [
                (220, 50), (405, 48), (485, 55), (570, 115),
                (680, 350), (675, 380), (555, 420), (542, 405),
                (548, 585), (540, 635), (505, 665), (440, 685),
                (350, 695), (250, 690), (180, 670), (145, 640),
                (145, 420), (125, 425), (15, 380), (8, 350),
                (65, 160), (125, 85),
            ],
        }
        allowed = np.zeros(gray.shape, np.uint8)
        cv2.fillPoly(allowed, [np.array(outlines[board], np.int32)], 255)
        bright = cv2.bitwise_and(bright, allowed)
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
            (board == "front")
            & (hull_mask > 0) & (mask == 0)
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
    first_side = clean_first_side()
    other_side = clean_other_side()
    front_tag = [[(327, 1054), (378, 1057), (369, 1117), (316, 1108)]]
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
    sources = [
        (BOARD, (0, 0, 700, 700), "front", None, None),
        (FRAME_DIR / "odin-voin-02.jpg", None, "", front_tag, None),
        (first_side, None, "", first_side_tag, duplicate_side_tag),
        (FRAME_DIR / "odin-voin-04.jpg", None, "", None, None),
        (BOARD, (700, 0, 1402, 700), "back", None, None),
        (FRAME_DIR / "odin-voin-06.jpg", None, "", None, None),
        (other_side, None, "", other_side_clean_panel, other_side_print_edge),
        (FRAME_DIR / "odin-voin-09.jpg", None, "", None, None),
    ]
    outputs = []
    for index, (source, crop, board, include, exclude) in enumerate(sources, 1):
        cutout = garment_cutout(
            source,
            crop=crop,
            board=board,
            include_polygons=include,
            exclude_polygons=exclude,
        )
        frame = composite(plate, cutout)
        if frame.size != CANVAS_SIZE:
            raise AssertionError(frame.size)
        output = SPIN / f"odin-voin-{index:02d}-v5.jpg"
        frame.save(output, quality=95, subsampling=0, optimize=True)
        outputs.append(output)
    return outputs


def main() -> None:
    for output in build_white_spin():
        print(output.relative_to(ROOT))


if __name__ == "__main__":
    main()
