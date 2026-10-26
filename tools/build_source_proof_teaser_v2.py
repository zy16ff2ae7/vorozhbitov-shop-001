#!/usr/bin/env python3
"""Build an 18-second source montage from the current catalog assets.

This is a slideshow/montage only; it does not simulate a 360-degree turn.
Every input is an original photo, direct client-source crop, or a frame from
verified white-tee turn footage. No generated black-tee render is included.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
OUTPUT = ASSETS / "video/drop-source-proof-v2.mp4"
SOURCES = [
    ASSETS / "drop/tee-real-8272.jpg",
    ASSETS / "product-proof/tee-sila-back-photo-v2.jpg",
    ASSETS / "drop/tee-real-8275.jpg",
    ASSETS / "product-proof/tee-vzglyad-front-source-v2.jpg",
    ASSETS / "product-proof/tee-vzglyad-back-source-v2.jpg",
    ASSETS / "product-proof/tee-vzglyad-layout-source-v2.jpg",
    ASSETS / "spin/odin-voin-concrete-000-v1.jpg",
    ASSETS / "spin/odin-voin-concrete-152-v1.jpg",
    ASSETS / "spin/odin-voin-concrete-266-v1.jpg",
]
SCENE_SECONDS = 2.125
FADE_SECONDS = 0.140625
FPS = 24


def ffmpeg() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def main() -> None:
    missing = [str(path) for path in SOURCES if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing source image(s): " + ", ".join(missing))
    command = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for source in SOURCES:
        command += ["-loop", "1", "-framerate", str(FPS), "-t", str(SCENE_SECONDS), "-i", str(source)]
    filters = []
    for index in range(len(SOURCES)):
        filters.append(
            f"[{index}:v]scale=1080:590:force_original_aspect_ratio=decrease,"
            f"pad=1080:590:(ow-iw)/2:(oh-ih)/2:color=0x080809,"
            f"fps={FPS},setsar=1,format=yuv420p[v{index}]"
        )
    previous = "v0"
    for index in range(1, len(SOURCES)):
        label = f"x{index}"
        offset = index * (SCENE_SECONDS - FADE_SECONDS)
        filters.append(
            f"[{previous}][v{index}]xfade=transition=fade:duration={FADE_SECONDS}:"
            f"offset={offset:.6f},fps={FPS}[{label}]"
        )
        previous = label
    filters.append(f"[{previous}]fps={FPS},format=yuv420p[proof]")
    duration = len(SOURCES) * SCENE_SECONDS - (len(SOURCES) - 1) * FADE_SECONDS
    command += [
        "-filter_complex", ";".join(filters), "-map", "[proof]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-frames:v", str(round(duration * FPS)), str(OUTPUT),
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)
    print(f"{OUTPUT.relative_to(ROOT)}: {duration:.1f}s source-only montage; not a 360 clip")


if __name__ == "__main__":
    main()
