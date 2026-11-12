#!/usr/bin/env python3
"""Build 100% authentic product media directly from original photoshoot and client assets.

No artificial cutouts, no fake inpainting, no synthetic drop shadows.
1. «СИЛА И ЧЕСТЬ» Front:
   - Authentic photoshoot flatlay (tee-flatlay-v2.jpg) framed to 1080x1350
     with natural concrete floor padding.
2. «СИЛА И ЧЕСТЬ» Back:
   - Authentic photoshoot photo of Nikita wearing the garment (tee-back-final.jpg, 1080x1350),
     color-graded with subtle midtone clarity so the distressed steel spine blade
     and woven collar monogram tag (•В•) stand out clearly.
3. «ВЗГЛЯД» Front:
   - Authentic client mockup (photo_18) front crop, framed to 1080x1350 on its native
     dark graphite studio background.
4. «ВЗГЛЯД» Back:
   - Authentic client mockup (photo_18) back crop with 10 principles circle,
     framed to 1080x1350 on its native dark graphite studio background.
5. «ВЗГЛЯД» Layout:
   - Verbatim copy of original photo_18.
"""
from __future__ import annotations

import shutil
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
PROOF = ASSETS / "product-proof"
DROP = ASSETS / "drop"

P18_PATH = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"
FLATLAY_PATH = DROP / "tee-flatlay-v2.jpg"
BACK_FINAL_PATH = DROP / "tee-back-final.jpg"

TARGET_W = 1080
TARGET_H = 1350


def build_sila_front() -> None:
    fl = cv2.imread(str(FLATLAY_PATH))
    if fl is None:
        raise FileNotFoundError(f"Missing flatlay: {FLATLAY_PATH}")
    # 1080x1080 -> 1080x1350 by reflecting natural concrete floor top & bottom
    fl_1350 = cv2.copyMakeBorder(fl, 135, 135, 0, 0, cv2.BORDER_REFLECT_101)
    out_path = PROOF / "tee-sila-front-photo-v1.jpg"
    cv2.imwrite(str(out_path), fl_1350, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print(f"Built {out_path.name}: {fl_1350.shape}")


def build_sila_back() -> None:
    bf = cv2.imread(str(BACK_FINAL_PATH))
    if bf is None:
        raise FileNotFoundError(f"Missing back final: {BACK_FINAL_PATH}")
    # Enhance midtones and shadow details using CLAHE on L channel
    lab = cv2.cvtColor(bf, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l)
    l_final = np.clip(l.astype(float) * 0.45 + l_enhanced.astype(float) * 0.55, 0, 255).astype(np.uint8)
    enhanced = cv2.cvtColor(cv2.merge([l_final, a, b]), cv2.COLOR_LAB2BGR)
    out_path = PROOF / "tee-sila-back-photo-v2.jpg"
    cv2.imwrite(str(out_path), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print(f"Built {out_path.name}: {enhanced.shape}")


def frame_vzglyad_view(raw_crop: np.ndarray, bg_color: np.ndarray) -> np.ndarray:
    gh, gw = raw_crop.shape[:2]
    scale = (TARGET_H * 0.82) / gh
    nw, nh = round(gw * scale), round(gh * scale)
    scaled = cv2.resize(raw_crop, (nw, nh), interpolation=cv2.INTER_LANCZOS4)

    canvas = np.full((TARGET_H, TARGET_W, 3), bg_color, dtype=np.uint8)
    x0 = (TARGET_W - nw) // 2
    y0 = (TARGET_H - nh) // 2

    # Smooth 12px feather blend at crop boundaries into the canvas
    feather = 12
    alpha_mask = np.ones((nh, nw), dtype=np.float32)
    for i in range(feather):
        factor = float(i) / float(feather)
        alpha_mask[i, :] = np.minimum(alpha_mask[i, :], factor)
        alpha_mask[nh - 1 - i, :] = np.minimum(alpha_mask[nh - 1 - i, :], factor)
        alpha_mask[:, i] = np.minimum(alpha_mask[:, i], factor)
        alpha_mask[:, nw - 1 - i] = np.minimum(alpha_mask[:, nw - 1 - i], factor)

    alpha_mask = alpha_mask[:, :, None]
    bg_patch = canvas[y0:y0+nh, x0:x0+nw].astype(np.float32)
    fg_patch = scaled.astype(np.float32)
    blended = np.clip(fg_patch * alpha_mask + bg_patch * (1.0 - alpha_mask), 0, 255).astype(np.uint8)
    canvas[y0:y0+nh, x0:x0+nw] = blended
    return canvas


def build_vzglyad() -> None:
    p18 = cv2.imread(str(P18_PATH))
    if p18 is None:
        raise FileNotFoundError(f"Missing P18: {P18_PATH}")

    # Native studio background color from top-left corner
    bg_color = np.median(p18[:25, :25], axis=(0, 1)).astype(np.uint8)

    # Front: left side garment crop (65..785, 0..670)
    front_raw = p18[65:785, 0:670]
    front_framed = frame_vzglyad_view(front_raw, bg_color)
    vf_path = PROOF / "tee-vzglyad-front-source-v2.jpg"
    cv2.imwrite(str(vf_path), front_framed, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print(f"Built {vf_path.name}: {front_framed.shape}")

    # Back: right side garment crop (65..785, 650..1280)
    back_raw = p18[65:785, 650:1280]
    back_framed = frame_vzglyad_view(back_raw, bg_color)
    vb_path = PROOF / "tee-vzglyad-back-source-v2.jpg"
    cv2.imwrite(str(vb_path), back_framed, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print(f"Built {vb_path.name}: {back_framed.shape}")

    # Full layout: copy verbatim
    layout_path = PROOF / "tee-vzglyad-layout-source-v2.jpg"
    shutil.copyfile(P18_PATH, layout_path)
    print(f"Copied {layout_path.name}: {p18.shape}")


def main() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    build_sila_front()
    build_sila_back()
    build_vzglyad()
    print("All authentic product media built successfully.")


if __name__ == "__main__":
    main()
