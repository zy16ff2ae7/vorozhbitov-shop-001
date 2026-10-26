#!/usr/bin/env python3
"""LEGACY builder for the archived source-proof-v1 media package.

This script regenerates the old Sila flatlay-derived proof frames and v1
montage; that Sila flatlay does not match the real photographed lettering and
is no longer used in the active catalog. Do not use this script for active
product or teaser media. The current teaser is built by
``tools/build_source_proof_teaser_v2.py``. These v1 montages are photo-based,
not 360-degree spins. Requires Pillow and imageio-ffmpeg.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
VIDEO_DIR = ASSETS / "video"
SPIN_DIR = ASSETS / "spin"
PROOF_DIR = ASSETS / "product-proof"
RAW_WHITE_VIDEO = ROOT / "task_materials/chat_export/video_files/0818.mp4"
FPS = 24
FRAME_SIZE = (1080, 590)

PRODUCTS = {
    "tee-sila-i-chest": [
        "tee-sila-layout-v1.jpg",
        "tee-sila-front-v1.jpg",
        "tee-sila-back-v1.jpg",
    ],
    "tee-vzglyad": [
        "tee-vzglyad-layout-v1.jpg",
        "tee-vzglyad-front-v1.jpg",
        "tee-vzglyad-back-v1.jpg",
    ],
    "tee-odin-v-pole-voin": [
        "tee-white-layout-v1.jpg",
        "tee-white-front-v1.jpg",
        "tee-white-back-v1.jpg",
    ],
}
PROOF_SOURCES = {
    "tee-sila-i-chest": ASSETS / "drop/tee-flatlay.jpg",
    "tee-vzglyad": ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg",
    "tee-odin-v-pole-voin": ROOT / "task_materials/chat_export/photos/photo_7@24-10-2026_14-40-12.jpg",
}
PROOF_CROPS = {
    "tee-sila-i-chest": ((0, 150, 400, 650), (420, 150, 800, 650)),
    "tee-vzglyad": ((0, 65, 690, 785), (640, 65, 1280, 785)),
    "tee-odin-v-pole-voin": ((0, 0, 700, 710), (700, 0, 1402, 710)),
}
# Retail order: show the garment front first, the back second, and the full
# source board last. PRODUCTS remains layout-first because it drives exact crops.
GALLERY_ORDER = {
    "tee-sila-i-chest": ("tee-sila-layout-v1.jpg", "tee-sila-front-v1.jpg", "tee-sila-back-v1.jpg"),
    "tee-vzglyad": ("tee-vzglyad-front-v1.jpg", "tee-vzglyad-back-v1.jpg", "tee-vzglyad-layout-v1.jpg"),
    "tee-odin-v-pole-voin": ("tee-white-front-v1.jpg", "tee-white-back-v1.jpg", "tee-white-layout-v1.jpg"),
}


def ffmpeg_executable() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def build_proof_images() -> None:
    """Copy each chosen layout verbatim and make only documented pixel crops."""
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    for product_id, names in PRODUCTS.items():
        source = PROOF_SOURCES[product_id]
        if not source.is_file():
            raise FileNotFoundError(f"Missing proof source: {source}")
        layout = PROOF_DIR / names[0]
        shutil.copyfile(source, layout)
        with Image.open(source) as image:
            for crop_box, name in zip(PROOF_CROPS[product_id], names[1:]):
                if crop_box[2] > image.width or crop_box[3] > image.height:
                    raise ValueError(f"Crop {crop_box} is outside source {source}")
                image.crop(crop_box).save(PROOF_DIR / name, quality=94, subsampling=0)


def sources_for(product_id: str) -> list[Path]:
    sources = [PROOF_DIR / name for name in GALLERY_ORDER[product_id]]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing proof source(s): " + ", ".join(missing))
    return sources


def render_video(sources: list[Path], output: Path, *, scene_seconds: float, fade: float) -> None:
    command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        command += ["-loop", "1", "-framerate", str(FPS), "-t", str(scene_seconds), "-i", str(source)]

    filters: list[str] = []
    for index in range(len(sources)):
        filters.append(
            f"[{index}:v]scale={FRAME_SIZE[0]}:{FRAME_SIZE[1]}:force_original_aspect_ratio=decrease,"
            f"pad={FRAME_SIZE[0]}:{FRAME_SIZE[1]}:(ow-iw)/2:(oh-ih)/2:color=0x080809,"
            f"fps={FPS},setsar=1,format=yuv420p[v{index}]"
        )

    previous = "v0"
    for index in range(1, len(sources)):
        label = f"x{index}"
        offset = index * (scene_seconds - fade)
        filters.append(
            f"[{previous}][v{index}]xfade=transition=fade:duration={fade}:"
            f"offset={offset:.6f},fps={FPS}[{label}]"
        )
        previous = label
    filters.append(f"[{previous}]fps={FPS},format=yuv420p[proof]")

    command += [
        "-filter_complex", ";".join(filters), "-map", "[proof]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-frames:v", str(round((len(sources) * scene_seconds - (len(sources) - 1) * fade) * FPS)),
        str(output),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)


def build_white_rotation_source() -> None:
    """Transcode the client's real white-tee turntable video and sample true angles.

    Only a centered black-background crop is applied to still frames so the shirt
    fits the product viewer. The untouched original remains under task_materials.
    """
    if not RAW_WHITE_VIDEO.is_file():
        raise FileNotFoundError(f"Missing client video: {RAW_WHITE_VIDEO}")
    SPIN_DIR.mkdir(parents=True, exist_ok=True)
    import imageio_ffmpeg
    frame_count, _ = imageio_ffmpeg.count_frames_and_secs(str(RAW_WHITE_VIDEO))
    if frame_count < 16:
        raise ValueError("Client rotation video is too short to sample eight angles")
    selected = {round(index * frame_count / 8) for index in range(8)}
    reader = imageio_ffmpeg.read_frames(str(RAW_WHITE_VIDEO), pix_fmt="rgb24")
    metadata = next(reader)
    width, height = metadata["size"]
    # Crop only empty black head/foot margin; the tee and print remain untouched.
    top = round(height * 0.15625)
    bottom = round(height * 0.85417)
    from PIL import Image
    for frame_index, raw_frame in enumerate(reader):
        if frame_index not in selected:
            continue
        image = Image.frombytes("RGB", (width, height), raw_frame)
        image.crop((0, top, width, bottom)).save(
            SPIN_DIR / f"odin-voin-source-{frame_index:03d}-v1.jpg", quality=95, subsampling=0
        )
    generated = sorted(SPIN_DIR.glob("odin-voin-source-*-v1.jpg"))
    if len(generated) != 8:
        raise RuntimeError(f"Expected eight sampled client angles, got {len(generated)}")

    output = VIDEO_DIR / "tee-odin-v-pole-voin-rotation-source-v1.mp4"
    command = [
        ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(RAW_WHITE_VIDEO), "-vf", "fps=24,scale=720:1280:flags=lanczos",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]
    subprocess.run(command, check=True)
    print(f"{output.relative_to(ROOT)}: source turntable transcode; {len(generated)} genuine angle frames")


def main() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    build_proof_images()
    proofs: dict[str, list[Path]] = {}
    for product_id, filenames in PRODUCTS.items():
        sources = sources_for(product_id)
        proofs[product_id] = sources
        output = VIDEO_DIR / f"{product_id}-proof-v1.mp4"
        render_video(sources, output, scene_seconds=2.8, fade=0.2)
        print(f"{output.relative_to(ROOT)}: 8.0 sec from {len(sources)} unchanged source views")

    build_white_rotation_source()

    film_sources = [source for product_id in PRODUCTS for source in proofs[product_id]]
    film = VIDEO_DIR / "drop-source-proof-v1.mp4"
    render_video(film_sources, film, scene_seconds=2.125, fade=0.140625)
    print(f"{film.relative_to(ROOT)}: source-only film from {len(film_sources)} proof views")

    # Verify that inputs remain independently decodable JPEGs; no source is
    # transformed in-place and every proof family remains versioned.
    for source in (item for group in proofs.values() for item in group):
        with Image.open(source) as image:
            image.verify()


if __name__ == "__main__":
    main()
