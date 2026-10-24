#!/usr/bin/env python3
"""Replace the old monogram in product review renders with the supplied mark.

The customer reference is preserved in ``task_materials/chat_export/photos``.
The extraction source is the existing, visually matching
``miniapp/assets/welcome-sword-logo.jpg``; its silhouette and distress are
recoloured, not redrawn.  Every edited image gets a new filename; customer
sources and earlier review versions remain untouched.  These are still review
renders, not approved production artwork.

Requires Pillow, NumPy and OpenCV (``opencv-python-headless``).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
SOURCE_LOGO = MINIAPP / "assets/welcome-sword-logo.jpg"
SPIN = MINIAPP / "assets/spin"
DROP = MINIAPP / "assets/drop"
BRAND = MINIAPP / "assets/brand"


def make_logo(variant: str) -> Image.Image:
    """Extract the exact distressed silhouette and recolour it light/dark."""
    source = np.asarray(Image.open(SOURCE_LOGO).convert("RGB"))
    luminance = cv2.cvtColor(source, cv2.COLOR_RGB2GRAY).astype(np.float32)
    ys, xs = np.where(luminance > 10)
    if not len(xs):
        raise RuntimeError(f"No logo found in {SOURCE_LOGO}")
    luminance = luminance[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    # Black in the supplied JPEG becomes transparency.  The variable alpha
    # retains the authentic worn/camouflage texture after recolouring.
    alpha = np.clip((luminance - 3) / 38, 0, 1) ** 0.62
    colour = 242 if variant == "light" else 18
    rgb = np.full((*luminance.shape, 3), colour, dtype=np.uint8)
    return Image.fromarray(np.dstack((rgb, (alpha * 255).astype(np.uint8))), "RGBA")


LOGOS = {name: make_logo(name) for name in ("light", "dark")}


def fitted_logo(variant: str, size: tuple[int, int], rotate: float = 0) -> Image.Image:
    """Fit without distorting the supplied mark's proportions."""
    width, height = size
    source = LOGOS[variant]
    scale = min(width / source.width, height / source.height)
    resized = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.Resampling.LANCZOS,
    )
    if rotate:
        resized = resized.rotate(rotate, Image.Resampling.BICUBIC, expand=True)
    canvas = Image.new("RGBA", size)
    canvas.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return canvas


def replace_logo(
    source: Path,
    target: Path,
    box: tuple[int, int, int, int],
    *,
    variant: str = "light",
    old: str = "white",
    rotate: float = 0,
    old_box: tuple[int, int, int, int] | None = None,
) -> None:
    """Inpaint the old print inside ``box`` and composite the supplied mark.

    ``old=white`` is for an old light print on black cloth, ``old=black`` for
    dark print on white cloth, and ``old=shape`` for metal where a luminance
    threshold would also select the natural shading of the dog tag.
    """
    base = Image.open(source).convert("RGB")
    pixels = np.asarray(base).copy()
    x, y, width, height = box
    if min(width, height) <= 0 or x < 0 or y < 0 or x + width > base.width or y + height > base.height:
        raise ValueError(f"Invalid logo box {box} for {source}")
    ox, oy, ow, oh = old_box or box
    roi = pixels[oy : oy + oh, ox : ox + ow]
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    if old == "white":
        old_mask = (gray > 62).astype(np.uint8) * 255
    elif old == "print":
        # Some original chest prints are dark red: their grayscale value is
        # below the white-ink threshold, so use the brightest colour channel.
        old_mask = (roi.max(axis=2) > 50).astype(np.uint8) * 255
    elif old == "black":
        old_mask = (gray < 125).astype(np.uint8) * 255
    elif old == "shape":
        old_mask = np.zeros_like(gray)
    else:
        raise ValueError(old)

    full_mask = np.zeros(pixels.shape[:2], np.uint8)
    full_mask[oy : oy + oh, ox : ox + ow] = old_mask
    logo = fitted_logo(variant, (width, height), rotate)
    shape_mask = (np.asarray(logo.getchannel("A")) > 8).astype(np.uint8) * 255
    full_mask[y : y + height, x : x + width] = cv2.bitwise_or(
        full_mask[y : y + height, x : x + width], shape_mask
    )
    full_mask = cv2.dilate(full_mask, np.ones((3, 3), np.uint8), iterations=1)
    cleaned = cv2.inpaint(pixels, full_mask, 4, cv2.INPAINT_TELEA)
    result = Image.fromarray(cleaned).convert("RGBA")
    result.alpha_composite(logo, (x, y))
    target.parent.mkdir(parents=True, exist_ok=True)
    result.convert("RGB").save(target, quality=96, subsampling=0)


def copy_or_edit(source: Path, target: Path, edits: list[dict]) -> None:
    if not edits:
        shutil.copy2(source, target)
        return
    # Apply each marked region to the previous result.  All regions in one
    # image are independent (for example chest monogram + hem patch).
    current = source
    temporary = target.with_suffix(".working.jpg")
    for index, edit in enumerate(edits):
        destination = target if index == len(edits) - 1 else temporary
        replace_logo(current, destination, **edit)
        current = destination
    temporary.unlink(missing_ok=True)


def main() -> None:
    BRAND.mkdir(exist_ok=True)
    for variant, logo in LOGOS.items():
        logo.save(BRAND / f"sword-monogram-{variant}.png")

    sich = {
        1: [(635, 475, 25, 42, 0)],
        2: [(649, 454, 20, 40, 0)],
        3: [(594, 480, 23, 40, 0)],
        4: [(465, 101, 24, 38, -6)],
        5: [(525, 99, 32, 44, 0)],
        6: [(586, 101, 25, 40, 6)],
        7: [(480, 475, 25, 40, 0)],
        8: [(562, 475, 28, 42, 0)],
    }
    for frame, regions in sich.items():
        edits = [dict(box=box, rotate=angle, old="print") for *box, angle in regions]
        copy_or_edit(SPIN / f"tee-sich-{frame:02d}-v2.jpg", SPIN / f"tee-sich-{frame:02d}-v3.jpg", edits)

    hem = {
        1: (635, 475, 25, 42),
        2: (649, 454, 20, 40),
        7: (570, 475, 25, 42),
        8: (562, 475, 28, 42),
    }
    chest = {
        1: (530, 258, 22, 32),
        2: (591, 253, 20, 32),
        8: (489, 245, 22, 32),
    }
    back = {
        4: (470, 150, 90, 255),
        5: (470, 150, 145, 270),
        6: (455, 150, 85, 250),
    }
    back_old = {
        4: (482, 155, 65, 245),
        6: (468, 155, 58, 240),
    }
    for frame in range(1, 9):
        edits = []
        if frame in chest:
            edits.append(dict(box=chest[frame]))
        if frame in hem:
            edits.append(dict(box=hem[frame]))
        if frame in back:
            # Angled backs need a tight old-print ROI so the value ring is not
            # mistaken for the monogram and inpainted with it.
            edits.append(dict(box=back[frame], old_box=back_old.get(frame)))
        copy_or_edit(SPIN / f"tee-vzglyad-{frame:02d}.jpg", SPIN / f"tee-vzglyad-{frame:02d}-v2.jpg", edits)

    replace_logo(DROP / "tee-detail-v4.jpg", DROP / "tee-detail-v5.jpg", (450, 220, 105, 160), old="print")
    replace_logo(DROP / "tee-vzglyad-detail.jpg", DROP / "tee-vzglyad-detail-v2.jpg", (500, 480, 80, 105))
    replace_logo(SPIN / "tag-sich-01.jpg", SPIN / "tag-sich-01-v2.jpg", (466, 190, 150, 290), variant="dark", old="shape")
    for frame in (2, 3, 4):
        shutil.copy2(SPIN / f"tag-sich-{frame:02d}.jpg", SPIN / f"tag-sich-{frame:02d}-v2.jpg")


if __name__ == "__main__":
    main()
