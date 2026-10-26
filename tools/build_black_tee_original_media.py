#!/usr/bin/env python3
"""Build unmasked, source-faithful media for the two black tees.

This script makes one direct crop of the real Sila front photo, crops the
supplied Vzglyad front/back views and Sila's back photo without removing
backgrounds, and copies the genuine Sila front-to-back source clip. It never
uses the incorrect Sila flatlay for product images and does not create 360
frames; the supplied clip is only a front-to-back turn.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "miniapp/assets/product-proof"
DROP = ROOT / "miniapp/assets/drop"
VIDEO_DIR = ROOT / "miniapp/assets/video"
VZGLYAD_SOURCE = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"
SILA_FRONT_SOURCE = DROP / "tee-real-8272.jpg"
SILA_BACK_SOURCE = DROP / "tee-back-v2.jpg"
SILA_VIDEO_SOURCE = ROOT / "task_materials/chat_export/video_files/video_1@24-10-2026_14-23-21.mp4"


def main() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    board = Image.open(VZGLYAD_SOURCE).convert("RGB")
    if board.size != (1280, 853):
        raise ValueError(f"Unexpected Vzglyad source size: {board.size}")
    outputs = [
        (board.crop((0, 65, 690, 785)), PROOF / "tee-vzglyad-front-source-v2.jpg"),
        (board.crop((700, 65, 1280, 785)), PROOF / "tee-vzglyad-back-source-v2.jpg"),
        (board, PROOF / "tee-vzglyad-layout-source-v2.jpg"),
    ]
    front = Image.open(SILA_FRONT_SOURCE).convert("RGB")
    outputs.append((front.crop((90, 410, 810, 1190)), PROOF / "tee-sila-front-photo-v1.jpg"))
    back = Image.open(SILA_BACK_SOURCE).convert("RGB")
    outputs.append((back.crop((160, 0, 920, 831)), PROOF / "tee-sila-back-photo-v2.jpg"))
    for image, output in outputs:
        image.save(output, quality=96, subsampling=0)
    video_output = VIDEO_DIR / "tee-sila-original-front-to-back-v1.mp4"
    shutil.copyfile(SILA_VIDEO_SOURCE, video_output)
    print("Built original, unmasked black-tee views and copied the authentic Sila front-to-back video:")
    for _, output in outputs:
        print(f"- {output.relative_to(ROOT)}")
    print(f"- {video_output.relative_to(ROOT)} (not a complete 360 turn)")


if __name__ == "__main__":
    main()
