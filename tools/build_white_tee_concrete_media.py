#!/usr/bin/env python3
"""Put the authentic white-tee source frames on the shared concrete backdrop.

The garment pixels are taken directly from the source rotation assets/video. The
only image operation is masking the near-black source background and compositing
onto a separate background plate. No garment design, print, or geometry is
redrawn. Requires the optional dependencies in requirements-media.txt.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
SPIN_DIR = ASSETS / "spin"
BG_PATH = ASSETS / "product-proof/white-tee-concrete-bg-v1.png"
SOURCE_VIDEO = ASSETS / "video/tee-odin-v-pole-voin-rotation-source-v1.mp4"
OUTPUT_VIDEO = ASSETS / "video/tee-odin-v-pole-voin-rotation-concrete-v1.mp4"
FRAME_IDS = (0, 38, 76, 114, 152, 190, 228, 266)


def background_for(background_rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    scale = max(width / background_rgb.shape[1], height / background_rgb.shape[0])
    resized = cv2.resize(
        background_rgb,
        (round(background_rgb.shape[1] * scale), round(background_rgb.shape[0] * scale)),
        interpolation=cv2.INTER_CUBIC,
    )
    left = (resized.shape[1] - width) // 2
    top = (resized.shape[0] - height) // 2
    return resized[top : top + height, left : left + width]


def garment_mask(image_rgb: np.ndarray) -> np.ndarray:
    """Keep the bright garment, fill enclosed prints, preserve open background.

    A few source angles clip black sewn labels at the image boundary. Tiny
    boundary-connected dark islands are included to retain those authentic
    labels; large open areas between sleeves and body stay transparent.
    """
    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    foreground = (gray > 18).astype(np.uint8) * 255
    foreground = cv2.morphologyEx(
        foreground,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)), 
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
    if count <= 1:
        raise ValueError("No bright garment component detected in source frame")
    garment_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = (labels == garment_label).astype(np.uint8)

    inverse = 1 - mask
    count, inverse_labels, inverse_stats, _ = cv2.connectedComponentsWithStats(inverse, 8)
    edge_labels = np.unique(
        np.concatenate(
            (
                inverse_labels[0, :],
                inverse_labels[-1, :],
                inverse_labels[:, 0],
                inverse_labels[:, -1],
            )
        )
    )
    exterior = np.isin(inverse_labels, edge_labels)
    enclosed_dark_prints = (inverse != 0) & ~exterior
    mask[enclosed_dark_prints] = 1

    # Preserve small dark sewn patches clipped by the frame edge without
    # mistakenly filling the broad black gaps below the sleeves.
    small_edge_area = max(64, int(image_rgb.shape[0] * image_rgb.shape[1] * 0.005))
    for label in edge_labels:
        label = int(label)
        if label == 0:
            continue
        if int(inverse_stats[label, cv2.CC_STAT_AREA]) <= small_edge_area:
            mask[inverse_labels == label] = 1

    return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 0.8)


def composite(image_rgb: np.ndarray, background_rgb: np.ndarray) -> np.ndarray:
    mask = garment_mask(image_rgb)[:, :, None]
    return np.clip(image_rgb * mask + background_rgb * (1 - mask), 0, 255).astype(np.uint8)


def build_angle_frames(background_rgb: np.ndarray) -> None:
    for frame_id in FRAME_IDS:
        source = SPIN_DIR / f"odin-voin-source-{frame_id:03d}-v1.jpg"
        output = SPIN_DIR / f"odin-voin-concrete-{frame_id:03d}-v1.jpg"
        image = np.asarray(Image.open(source).convert("RGB"))
        background = background_for(background_rgb, (image.shape[1], image.shape[0]))
        Image.fromarray(composite(image, background)).save(output, quality=95, subsampling=0)


def build_rotation_video(background_rgb: np.ndarray) -> None:
    import imageio_ffmpeg

    capture = cv2.VideoCapture(str(SOURCE_VIDEO))
    fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0 or total <= 0:
        raise RuntimeError(f"Cannot decode source rotation video: {SOURCE_VIDEO}")
    background = background_for(background_rgb, (width, height))
    writer = imageio_ffmpeg.write_frames(
        str(OUTPUT_VIDEO),
        (width, height),
        fps=fps,
        codec="libx264",
        pix_fmt_out="yuv420p",
        output_params=["-preset", "medium", "-crf", "22", "-movflags", "+faststart"],
    )
    writer.send(None)
    count = 0
    try:
        while True:
            ok, frame_bgr = capture.read()
            if not ok:
                break
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            result = composite(frame_rgb, background)
            writer.send(np.ascontiguousarray(result))
            count += 1
    finally:
        capture.release()
        writer.close()
    if count != total:
        raise RuntimeError(f"Expected {total} decoded frames; processed {count}")


def main() -> None:
    background = np.asarray(Image.open(BG_PATH).convert("RGB"))
    build_angle_frames(background)
    build_rotation_video(background)
    print(f"Built eight source angles and {OUTPUT_VIDEO.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
