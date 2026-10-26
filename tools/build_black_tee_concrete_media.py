#!/usr/bin/env python3
"""Build unified concrete-background product card media for the black tees.

Puts the authentic black tees on the shared brutalist concrete backdrop plate
(white-tee-concrete-bg-v1.png), perfectly unified with the white tee (Odin v pole voin)
in 1080x1340 4:5 resolution, scale, centering, and ambient contact shadow.
"""
from __future__ import annotations

from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
PROOF = ASSETS / "product-proof"
DROP = ASSETS / "drop"
BRAND = ASSETS / "brand"
BG_PATH = PROOF / "white-tee-concrete-bg-v1.png"
VZGLYAD_SOURCE = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"

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


def composite_to_wall(garment_rgb: np.ndarray, mask: np.ndarray, target_canvas: np.ndarray) -> np.ndarray:
    scale = min(1040 / 690, 1220 / 720)
    rw = round(garment_rgb.shape[1] * scale)
    rh = round(garment_rgb.shape[0] * scale)
    im_s = cv2.resize(garment_rgb, (rw, rh), interpolation=cv2.INTER_CUBIC)
    m_s = cv2.resize(mask, (rw, rh), interpolation=cv2.INTER_LINEAR)

    ox = (1080 - rw) // 2
    oy = 134 + (1205 - rh) // 2

    shadow = np.zeros((1340, 1080), dtype=np.float32)
    shadow[oy + 14 : oy + rh + 14, ox : ox + rw] = m_s.astype(np.float32) / 255.0
    shadow = cv2.GaussianBlur(shadow, (49, 49), 22.0)

    canvas = target_canvas.copy().astype(np.float32)
    canvas = canvas * (1.0 - 0.45 * shadow[:, :, None])

    alpha = cv2.GaussianBlur(m_s.astype(np.float32) / 255.0, (5, 5), 1.2)[:, :, None]
    roi = canvas[oy : oy + rh, ox : ox + rw]
    canvas[oy : oy + rh, ox : ox + rw] = im_s * alpha + roi * (1.0 - alpha)
    return np.clip(canvas, 0, 255).astype(np.uint8)


def main() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    bg_full = Image.open(BG_PATH).convert("RGB")
    bg = np.array(bg_full.resize((1080, 1340), Image.Resampling.LANCZOS))

    p18 = np.array(Image.open(VZGLYAD_SOURCE))
    front_v = p18[65:785, 0:690].copy()
    back_v = p18[65:785, 700:1280].copy()

    mask_front = np.zeros((720, 690), dtype=np.uint8)
    cv2.fillPoly(mask_front, [np.array(VZGLYAD_FRONT, dtype=np.int32)], 255)

    mask_back = np.zeros((720, 580), dtype=np.uint8)
    cv2.fillPoly(mask_back, [np.array(VZGLYAD_BACK, dtype=np.int32)], 255)

    # Clean chest on front_v for Sila i Chest
    print_mask = np.zeros((720, 690), dtype=np.uint8)
    print_mask[220:520, 170:540] = (cv2.cvtColor(front_v[220:520, 170:540], cv2.COLOR_RGB2GRAY) > 28).astype(np.uint8) * 255
    print_mask = cv2.dilate(print_mask, np.ones((5, 5), np.uint8), iterations=2)
    clean_front_s = cv2.inpaint(front_v, print_mask, 5, cv2.INPAINT_TELEA)

    # Clean back for Sila i Chest back
    print_mask_b = np.zeros((720, 580), dtype=np.uint8)
    print_mask_b[180:550, 140:460] = (cv2.cvtColor(back_v[180:550, 140:460], cv2.COLOR_RGB2GRAY) > 28).astype(np.uint8) * 255
    print_mask_b = cv2.dilate(print_mask_b, np.ones((5, 5), np.uint8), iterations=2)
    clean_back_s = cv2.inpaint(back_v, print_mask_b, 5, cv2.INPAINT_TELEA)

    # Overlay Cyrillic chest wordmark
    wordmark = Image.open(BRAND / "sila-chest-wordmark.png")
    wm_w = 330
    wm_h = round(wordmark.height * (wm_w / wordmark.width))
    wm_resized = wordmark.resize((wm_w, wm_h), Image.Resampling.LANCZOS)

    img_clean_f = Image.fromarray(clean_front_s)
    wm_x = 345 - wm_w // 2
    wm_y = 265
    img_clean_f.paste(wm_resized, (wm_x, wm_y), wm_resized)

    # Overlay sword insignia on back
    sword = Image.open(BRAND / "sword-monogram-light-v2.png")
    sw_h = 360
    sw_w = round(sword.width * (sw_h / sword.height))
    sw_resized = sword.resize((sw_w, sw_h), Image.Resampling.LANCZOS)

    img_clean_b = Image.fromarray(clean_back_s)
    sw_x = 295 - sw_w // 2
    sw_y = 190
    img_clean_b.paste(sw_resized, (sw_x, sw_y), sw_resized)

    # Overlay red hem label
    flatlay = Image.open(DROP / "tee-flatlay-v2.jpg")
    hem_patch = flatlay.crop((475, 785, 515, 850))
    hem_w = 26
    hem_h = round(hem_patch.height * (hem_w / hem_patch.width))
    hem_resized = hem_patch.resize((hem_w, hem_h), Image.Resampling.LANCZOS)
    img_clean_f.paste(hem_resized, (158, 665))

    front_sila = np.array(img_clean_f)
    back_sila = np.array(img_clean_b)

    res_sila_f = composite_to_wall(front_sila, mask_front, bg)
    res_sila_b = composite_to_wall(back_sila, mask_back, bg)
    res_vzglyad_f = composite_to_wall(front_v, mask_front, bg)
    res_vzglyad_b = composite_to_wall(back_v, mask_back, bg)

    Image.fromarray(res_sila_f).save(PROOF / "tee-sila-front-photo-v1.jpg", quality=96, subsampling=0)
    Image.fromarray(res_sila_b).save(PROOF / "tee-sila-back-photo-v2.jpg", quality=96, subsampling=0)
    Image.fromarray(res_vzglyad_f).save(PROOF / "tee-vzglyad-front-source-v2.jpg", quality=96, subsampling=0)
    Image.fromarray(res_vzglyad_b).save(PROOF / "tee-vzglyad-back-source-v2.jpg", quality=96, subsampling=0)

    print("Built unified concrete product media (1080x1340) matching the white tee system:")
    print("- tee-sila-front-photo-v1.jpg")
    print("- tee-sila-back-photo-v2.jpg")
    print("- tee-vzglyad-front-source-v2.jpg")
    print("- tee-vzglyad-back-source-v2.jpg")


if __name__ == "__main__":
    main()
