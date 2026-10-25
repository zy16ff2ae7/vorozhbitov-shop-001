#!/usr/bin/env python3
"""Legacy experiment for concrete-background mask composites; not catalog media.

The mask approach produced unwanted light fringes around the black shirts, so
these outputs are not active. The catalog now uses the original unmasked photos
and the authentic Sila front-to-back source clip, built by
``tools/build_black_tee_original_media.py``. This file remains only for review
and for the historical ``video_scene`` canvas-sizing experiment. Requires
requirements-media.txt dependencies.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
PROOF = ASSETS / "product-proof"
BG_PATH = PROOF / "white-tee-concrete-bg-v1.png"
VIDEO_DIR = ASSETS / "video"
VIDEO_SIZE = (720, 1280)
CARD_SIZE = (720, 896)
VIDEO_FPS = 24
VZGLYAD_SOURCE = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"

# Pixel coordinates in the existing source crops (front x=0..690, back x=700..1280;
# both are cropped vertically at y=65..785). Points follow only the shirt edge.
VZGLYAD_FRONT = [
    (277, 23), (283, 30), (292, 37), (305, 43), (322, 48), (340, 51),
    (359, 51), (378, 48), (395, 42), (410, 34), (418, 24),
    (451, 42), (487, 49), (520, 61), (551, 78), (581, 103),
    (605, 135), (631, 186), (657, 253), (689, 340), (680, 350),
    (588, 381), (558, 390), (559, 694), (546, 707), (151, 707),
    (140, 687), (136, 389), (109, 381), (15, 349), (18, 337),
    (61, 240), (86, 178), (105, 132), (126, 98), (151, 74),
    (183, 56), (216, 45), (255, 40),
]
VZGLYAD_BACK = [
    (220, 23), (232, 27), (249, 30), (270, 32), (293, 33), (315, 32),
    (334, 30), (347, 26), (354, 24),
    (374, 36), (404, 46), (433, 62), (464, 86), (490, 112),
    (515, 147), (538, 190), (557, 245), (578, 300), (568, 326),
    (527, 350), (480, 378), (454, 400), (456, 687), (447, 704),
    (60, 704), (48, 680), (51, 401), (25, 383), (0, 365), (0, 322),
    (12, 251), (31, 198), (52, 156), (75, 121), (102, 91),
    (132, 68), (163, 49), (199, 38),
]


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


def fill_interior(mask: np.ndarray) -> np.ndarray:
    """Fill small enclosed print marks, but keep large gaps between garments transparent."""
    inverse = 1 - mask
    _, labels, stats, _ = cv2.connectedComponentsWithStats(inverse, 8)
    edge_labels = np.unique(
        np.concatenate((labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]))
    )
    enclosed_labels = np.setdiff1d(np.arange(1, len(stats)), edge_labels)
    max_print_hole = max(64, round(mask.size * 0.02))
    result = mask.copy()
    for label in enclosed_labels:
        if stats[label, cv2.CC_STAT_AREA] <= max_print_hole:
            result[labels == label] = 1
    return result


def dark_garment_on_light_source(image_rgb: np.ndarray) -> np.ndarray:
    """Extract Sila's black shirts from the original bright concrete layout."""
    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    binary = (gray < 120).astype(np.uint8)
    binary = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        raise ValueError("No dark garment component found in Sila source")
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return fill_interior((labels == largest).astype(np.uint8))


def polygon_mask(size: tuple[int, int], polygons: list[list[tuple[int, int]]]) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    for polygon in polygons:
        cv2.fillPoly(mask, [np.asarray(polygon, dtype=np.int32)], 1)
    return mask


def composite(image_rgb: np.ndarray, mask: np.ndarray, background_rgb: np.ndarray) -> np.ndarray:
    alpha = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 0.8)[:, :, None]
    return np.clip(image_rgb * alpha + background_rgb * (1 - alpha), 0, 255).astype(np.uint8)


def build_sila(background: np.ndarray) -> tuple[list[Path], list[tuple[np.ndarray, np.ndarray]]]:
    outputs: list[Path] = []
    scenes: list[tuple[np.ndarray, np.ndarray]] = []
    for source_name, output_name in (
        ("tee-sila-layout-v1.jpg", "tee-sila-layout-concrete-v1.jpg"),
        ("tee-sila-front-v1.jpg", "tee-sila-front-concrete-v1.jpg"),
        ("tee-sila-back-v1.jpg", "tee-sila-back-concrete-v1.jpg"),
    ):
        source = PROOF / source_name
        image = np.asarray(Image.open(source).convert("RGB"))
        mask = dark_garment_on_light_source(image)
        plate = background_for(background, (image.shape[1], image.shape[0]))
        output = PROOF / output_name
        Image.fromarray(composite(image, mask, plate)).save(output, quality=95, subsampling=0)
        outputs.append(output)
        scenes.append((image, mask))
    return outputs, scenes


def build_vzglyad(background: np.ndarray) -> tuple[list[Path], list[tuple[np.ndarray, np.ndarray]]]:
    original = np.asarray(Image.open(VZGLYAD_SOURCE).convert("RGB"))
    # Exact documented front crop and a shifted back crop that avoids the
    # neighboring front sleeve while keeping the original shirt artwork intact.
    front = original[65:785, 0:690]
    back = original[65:785, 700:1280]
    if front.shape[:2] != (720, 690) or back.shape[:2] != (720, 580):
        raise ValueError("Unexpected source photo 18 dimensions/crop bounds")

    offset_back = [(x + 700, y) for x, y in VZGLYAD_BACK]
    full_mask = polygon_mask((original.shape[1], original.shape[0]), [
        [(x, y + 65) for x, y in VZGLYAD_FRONT],
        [(x, y + 65) for x, y in offset_back],
    ])
    front_mask = polygon_mask((front.shape[1], front.shape[0]), [VZGLYAD_FRONT])
    back_mask = polygon_mask((back.shape[1], back.shape[0]), [VZGLYAD_BACK])

    outputs: list[Path] = []
    scenes: list[tuple[np.ndarray, np.ndarray]] = []
    for image, mask, filename in (
        (front, front_mask, "tee-vzglyad-front-concrete-v1.jpg"),
        (back, back_mask, "tee-vzglyad-back-concrete-v1.jpg"),
        (original, full_mask, "tee-vzglyad-layout-concrete-v1.jpg"),
    ):
        plate = background_for(background, (image.shape[1], image.shape[0]))
        output = PROOF / filename
        Image.fromarray(composite(image, mask, plate)).save(output, quality=95, subsampling=0)
        outputs.append(output)
        scenes.append((image, mask))
    return outputs, scenes


def video_scene(
    image_rgb: np.ndarray,
    mask: np.ndarray,
    background: np.ndarray,
    canvas_size: tuple[int, int] = VIDEO_SIZE,
) -> np.ndarray:
    """Place an unwarped source garment crop on a portrait wall canvas."""
    width, height = canvas_size
    max_width, max_height = width - 40, height - 60
    scale = min(max_width / image_rgb.shape[1], max_height / image_rgb.shape[0])
    resized_width = max(1, round(image_rgb.shape[1] * scale))
    resized_height = max(1, round(image_rgb.shape[0] * scale))
    image = cv2.resize(image_rgb, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    alpha = cv2.resize(mask.astype(np.float32), (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    alpha = cv2.GaussianBlur(alpha, (0, 0), 0.7)[:, :, None]
    canvas = background_for(background, canvas_size).copy()
    left = (width - resized_width) // 2
    top = (height - resized_height) // 2
    roi = canvas[top : top + resized_height, left : left + resized_width]
    canvas[top : top + resized_height, left : left + resized_width] = np.clip(
        image * alpha + roi * (1 - alpha), 0, 255
    ).astype(np.uint8)
    return canvas


def render_portrait_source_video(scene: tuple[np.ndarray, np.ndarray], background: np.ndarray, output: Path) -> None:
    """Create a portrait video from one source view with a subtle camera push-in.

    No frames are blended or invented: the movement is a crop of the same
    source-derived composite, not a turntable or a simulated garment rotation.
    """
    import imageio_ffmpeg

    base = video_scene(scene[0], scene[1], background)
    width, height = VIDEO_SIZE
    writer = imageio_ffmpeg.write_frames(
        str(output), VIDEO_SIZE, fps=VIDEO_FPS, codec="libx264", pix_fmt_out="yuv420p",
        output_params=["-preset", "medium", "-crf", "22", "-movflags", "+faststart"],
    )
    writer.send(None)
    try:
        total_frames = 8 * VIDEO_FPS
        for frame_number in range(total_frames):
            progress = frame_number / max(1, total_frames - 1)
            eased = (1 - np.cos(np.pi * progress)) / 2
            zoom = 1.0 + 0.035 * eased
            scaled = cv2.resize(
                base,
                (round(width * zoom), round(height * zoom)),
                interpolation=cv2.INTER_CUBIC,
            )
            left = (scaled.shape[1] - width) // 2
            top = (scaled.shape[0] - height) // 2
            frame = scaled[top : top + height, left : left + width]
            writer.send(np.ascontiguousarray(frame))
    finally:
        writer.close()


def build_card_image(scene: tuple[np.ndarray, np.ndarray], background: np.ndarray, output: Path) -> None:
    frame = video_scene(scene[0], scene[1], background, CARD_SIZE)
    Image.fromarray(frame).save(output, quality=95, subsampling=0)


def main() -> None:
    background = np.asarray(Image.open(BG_PATH).convert("RGB"))
    sila_outputs, sila_scenes = build_sila(background)
    vzglyad_outputs, vzglyad_scenes = build_vzglyad(background)
    card_outputs = (PROOF / "tee-vzglyad-card-concrete-v2.jpg",)
    build_card_image(vzglyad_scenes[0], background, card_outputs[0])
    video_outputs = (
        VIDEO_DIR / "tee-sila-i-chest-proof-concrete-v2.mp4",
        VIDEO_DIR / "tee-vzglyad-proof-concrete-v2.mp4",
    )
    render_portrait_source_video(sila_scenes[0], background, video_outputs[0])
    render_portrait_source_video(vzglyad_scenes[0], background, video_outputs[1])
    print("Built six exact-source black-tee views, one clean Vzglyad card portrait, and two portrait source-view videos on the shared concrete background:")
    for path in [*sila_outputs, *vzglyad_outputs, *card_outputs, *video_outputs]:
        print(f"- {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
