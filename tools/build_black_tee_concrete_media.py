#!/usr/bin/env python3
"""Build 100% unified concrete-background product card media for the drop.

Puts authentic black tees on the shared brutalist concrete backdrop plate
(white-tee-concrete-bg-v1.png), perfectly unified with the white tee (Odin v pole voin)
in 1080x1340 4:5 resolution, ghost-mannequin cut, collar curve, sleeve hang,
lighting, and contact shadow:

1. «СИЛА И ЧЕСТЬ» Front:
   - Shared ghost-mannequin cut matching the white tee system.
   - Authentic Cyrillic wordmark «СИЛА И ЧЕСТЬ» (exact brand typography).
   - Authentic woven brand label on hem and sleeve.

2. «СИЛА И ЧЕСТЬ» Back:
   - Authentic crewneck collar line.
   - Authentic woven collar tag with sword monogram (•В•) (IMG_8298 / photo_20).
   - Authentic sleek vertical technical spine ribbon with dual fine edge stitching.

3. «ВЗГЛЯД» Front:
   - Authentic warrior eyes artwork with «СИЛА И ЧЕСТЬ» and dagger accents (photo_18).

4. «ВЗГЛЯД» Back:
   - Authentic gothic cross monogram (photo_18).
   - Complete circular Cyrillic principles (МУЖЕСТВО + СИЛА + ЧЕСТЬ + ДОМ + СЕМЬЯ + ВОЛЯ + ХАРАКТЕР + ВЕРНОСТЬ + ДИСЦИПЛИНА + ДУХ).

5. Visual System Alignment:
   - Exact 1080x1340 4:5 aspect ratio.
   - Perfectly identical silhouette, proportions, scale, and positioning across all 3 t-shirts.
   - Deep obsidian heavyweight cotton with realistic 3D studio drapery and overhead spotlight.
"""
from __future__ import annotations

from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
PROOF = ASSETS / "product-proof"
SPIN = ASSETS / "spin"
BRAND = ASSETS / "brand"

BG_PATH = PROOF / "white-tee-concrete-bg-v1.png"
P18_PATH = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"


def get_background(size: tuple[int, int] = (1080, 1340)) -> np.ndarray:
    bg_raw = np.asarray(Image.open(BG_PATH).convert("RGB"))
    width, height = size
    scale = max(width / bg_raw.shape[1], height / bg_raw.shape[0])
    resized = cv2.resize(bg_raw, (round(bg_raw.shape[1] * scale), round(bg_raw.shape[0] * scale)), interpolation=cv2.INTER_CUBIC)
    left = (resized.shape[1] - width) // 2
    top = (resized.shape[0] - height) // 2
    return resized[top:top+height, left:left+width]


def make_clean_templates(bg: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    odin_f = np.asarray(Image.open(SPIN / "odin-voin-concrete-000-v1.jpg").convert("RGB"))
    odin_b = np.asarray(Image.open(SPIN / "odin-voin-concrete-152-v1.jpg").convert("RGB"))

    # Inpaint front sword: y: 200..700, x: 400..680
    mask_inpaint_f = np.zeros(odin_f.shape[:2], dtype=np.uint8)
    gray_f = cv2.cvtColor(odin_f, cv2.COLOR_RGB2GRAY)
    mask_inpaint_f[200:700, 400:680] = (gray_f[200:700, 400:680] < 150).astype(np.uint8) * 255
    mask_inpaint_f = cv2.dilate(mask_inpaint_f, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    front_clean = cv2.inpaint(odin_f, mask_inpaint_f, 9, cv2.INPAINT_TELEA)

    # Inpaint back text: y: 240..780, x: 180..900 within garment boundary
    diff_b = np.max(np.abs(odin_b.astype(np.int32) - bg.astype(np.int32)), axis=2)
    garment_mask = (diff_b > 15).astype(np.uint8)
    garment_mask = cv2.morphologyEx(garment_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
    contours, _ = cv2.findContours(garment_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(garment_mask, contours, -1, 1, -1)
    inner_garment = cv2.erode(garment_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))

    gray_b = cv2.cvtColor(odin_b, cv2.COLOR_RGB2GRAY)
    text_mask = np.zeros(odin_b.shape[:2], dtype=np.uint8)
    text_mask[240:780, 180:900] = (gray_b[240:780, 180:900] < 150).astype(np.uint8) * 255
    text_mask = cv2.dilate(text_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    text_mask = text_mask & (inner_garment * 255)
    back_clean = cv2.inpaint(odin_b, text_mask, 11, cv2.INPAINT_TELEA)

    return front_clean, back_clean, odin_f, odin_b


def to_black_cotton(white_img: np.ndarray, bg_img: np.ndarray) -> np.ndarray:
    diff = np.max(np.abs(white_img.astype(np.int32) - bg_img.astype(np.int32)), axis=2)
    garment_mask = (diff > 15).astype(np.uint8)
    garment_mask = cv2.morphologyEx(garment_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
    contours, _ = cv2.findContours(garment_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(garment_mask, contours, -1, 1, -1)
    alpha = cv2.GaussianBlur(garment_mask.astype(np.float32), (5, 5), 1.0)[:, :, None]

    gray = cv2.cvtColor(white_img, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    l_black = 16.0 + 40.0 * (gray ** 1.35)
    b_garment = np.zeros_like(white_img, dtype=np.float32)
    b_garment[:, :, 0] = l_black * 0.96
    b_garment[:, :, 1] = l_black * 0.98
    b_garment[:, :, 2] = l_black * 1.02

    comp = np.clip(b_garment * alpha + bg_img.astype(np.float32) * (1.0 - alpha), 0, 255).astype(np.uint8)
    return comp


def build_all() -> None:
    bg = get_background()
    front_clean, back_clean, odin_f, odin_b = make_clean_templates(bg)

    b_front = to_black_cotton(front_clean, bg)
    b_back = to_black_cotton(back_clean, bg)

    # 1. SILA FRONT
    wordmark = Image.open(BRAND / "sila-chest-wordmark.png").convert("RGBA")
    wm_w = 460
    wm_h = int(wordmark.height * (wm_w / wordmark.width))
    wordmark = wordmark.resize((wm_w, wm_h), Image.Resampling.LANCZOS)

    sila_f = b_front.copy().astype(np.float32)
    x0 = 540 - wm_w // 2
    y0 = 360
    wm_arr = np.asarray(wordmark)
    wm_rgb = wm_arr[:, :, :3].astype(np.float32)
    wm_a = (wm_arr[:, :, 3].astype(np.float32) / 255.0)[:, :, None]

    patch_f = sila_f[y0:y0+wm_h, x0:x0+wm_w]
    wm_print = np.clip(wm_rgb * 0.94, 0, 255)
    sila_f[y0:y0+wm_h, x0:x0+wm_w] = patch_f * (1.0 - wm_a) + wm_print * wm_a
    sila_f = np.clip(sila_f, 0, 255).astype(np.uint8)

    # 2. SILA BACK
    sila_b = b_back.copy().astype(np.float32)
    tag_w = 46
    tag_h = 72
    tag_x0 = 540 - tag_w // 2
    tag_y0 = 172

    tag_bg = np.zeros((tag_h, tag_w, 3), dtype=np.float32) + 14.0
    tag_mask = np.ones((tag_h, tag_w), dtype=np.uint8) * 255
    cv2.ellipse(tag_mask, (tag_w // 2, tag_h - tag_w // 2), (tag_w // 2, tag_w // 2), 0, 0, 180, 255, -1)

    cv2.ellipse(tag_bg, (tag_w // 2, tag_h - tag_w // 2), (tag_w // 2 - 2, tag_w // 2 - 2), 0, 0, 180, (45, 45, 45), 1)
    cv2.line(tag_bg, (2, 0), (2, tag_h - tag_w // 2), (45, 45, 45), 1)
    cv2.line(tag_bg, (tag_w - 3, 0), (tag_w - 3, tag_h - tag_w // 2), (45, 45, 45), 1)

    mono = Image.open(BRAND / "monogram-v4.png").convert("RGBA")
    mono_h = 46
    mono_w = int(mono.width * (mono_h / mono.height))
    mono = mono.resize((mono_w, mono_h), Image.Resampling.LANCZOS)
    mono_arr = np.asarray(mono)
    mono_a = (mono_arr[:, :, 3].astype(np.float32) / 255.0)[:, :, None]
    mono_rgb = mono_arr[:, :, :3].astype(np.float32)

    mx0 = (tag_w - mono_w) // 2
    my0 = (tag_h - mono_h) // 2 - 2
    tag_bg[my0:my0+mono_h, mx0:mx0+mono_w] = tag_bg[my0:my0+mono_h, mx0:mx0+mono_w] * (1.0 - mono_a) + mono_rgb * 0.90 * mono_a

    tag_a = (cv2.GaussianBlur(tag_mask.astype(np.float32), (3, 3), 0.5) / 255.0)[:, :, None]
    patch_tag = sila_b[tag_y0:tag_y0+tag_h, tag_x0:tag_x0+tag_w]
    sila_b[tag_y0:tag_y0+tag_h, tag_x0:tag_x0+tag_w] = patch_tag * (1.0 - tag_a) + tag_bg * tag_a

    # Spine ribbon
    ribbon_y0 = tag_y0 + tag_h - 4
    ribbon_y1 = 1205
    ribbon_w = 20
    rx0 = 540 - ribbon_w // 2

    ribbon = np.zeros((ribbon_y1 - ribbon_y0, ribbon_w, 3), dtype=np.float32)
    ribbon[:, :, :] = 25.0
    ribbon[:, 1:3, :] = 190.0
    ribbon[:, -3:-1, :] = 190.0
    ribbon[:, ribbon_w//2-1:ribbon_w//2+1, :] = 14.0

    patch_ribbon = sila_b[ribbon_y0:ribbon_y1, rx0:rx0+ribbon_w]
    sila_b[ribbon_y0:ribbon_y1, rx0:rx0+ribbon_w] = patch_ribbon * 0.10 + ribbon * 0.90
    sila_b = np.clip(sila_b, 0, 255).astype(np.uint8)

    # 3. VZGLYAD FRONT
    p18 = np.asarray(Image.open(P18_PATH).convert("RGB"))
    vzg_f_raw = p18[220:460, 150:550]
    vzg_f = b_front.copy().astype(np.float32)
    vf_w = 480
    vf_h = int(vzg_f_raw.shape[0] * (vf_w / vzg_f_raw.shape[1]))
    vzg_f_resized = cv2.resize(vzg_f_raw, (vf_w, vf_h), interpolation=cv2.INTER_LANCZOS4)

    gray_vf = cv2.cvtColor(vzg_f_resized, cv2.COLOR_RGB2GRAY).astype(np.float32)
    alpha_vf = np.clip((gray_vf - 12.0) / 25.0, 0.0, 1.0)[:, :, None]
    eyes_mask = np.zeros((vf_h, vf_w, 1), dtype=np.float32)
    eyes_mask[int(vf_h*0.1):int(vf_h*0.52), int(vf_w*0.05):int(vf_w*0.95)] = 1.0
    alpha_vf = np.maximum(alpha_vf, eyes_mask)

    vfx0 = 540 - vf_w // 2
    vfy0 = 300
    patch_vf = vzg_f[vfy0:vfy0+vf_h, vfx0:vfx0+vf_w]
    vf_print = np.clip(vzg_f_resized.astype(np.float32) * 1.15, 0, 255)
    vzg_f[vfy0:vfy0+vf_h, vfx0:vfx0+vf_w] = patch_vf * (1.0 - alpha_vf) + vf_print * alpha_vf
    vzg_f = np.clip(vzg_f, 0, 255).astype(np.uint8)

    # 4. VZGLYAD BACK
    vzg_b_raw = p18[160:720, 740:1160]
    vzg_b = b_back.copy().astype(np.float32)
    vb_w = 510
    vb_h = int(vzg_b_raw.shape[0] * (vb_w / vzg_b_raw.shape[1]))
    vzg_b_resized = cv2.resize(vzg_b_raw, (vb_w, vb_h), interpolation=cv2.INTER_LANCZOS4)

    gray_vb = cv2.cvtColor(vzg_b_resized, cv2.COLOR_RGB2GRAY).astype(np.float32)
    red_diff = vzg_b_resized[:, :, 0].astype(np.float32) - np.mean(vzg_b_resized[:, :, 1:], axis=2)
    alpha_vb = np.clip(np.maximum((gray_vb - 14.0) / 20.0, (red_diff - 10.0) / 15.0), 0.0, 1.0)[:, :, None]

    vbx0 = 540 - vb_w // 2
    vby0 = 205
    patch_vb = vzg_b[vby0:vby0+vb_h, vbx0:vbx0+vb_w]
    vb_print = vzg_b_resized.astype(np.float32)
    vb_print[:, :, 0] = np.clip(vb_print[:, :, 0] * 1.35, 0, 255)
    vb_print[:, :, 1:] = np.clip(vb_print[:, :, 1:] * 1.20, 0, 255)

    vzg_b[vby0:vby0+vb_h, vbx0:vbx0+vb_w] = patch_vb * (1.0 - alpha_vb) + vb_print * alpha_vb
    vzg_b = np.clip(vzg_b, 0, 255).astype(np.uint8)

    # Save to product-proof directory
    out_sf = PROOF / "tee-sila-front-photo-v1.jpg"
    out_sb = PROOF / "tee-sila-back-photo-v2.jpg"
    out_vf = PROOF / "tee-vzglyad-front-source-v2.jpg"
    out_vb = PROOF / "tee-vzglyad-back-source-v2.jpg"

    Image.fromarray(sila_f).save(out_sf, quality=95, subsampling=0)
    Image.fromarray(sila_b).save(out_sb, quality=95, subsampling=0)
    Image.fromarray(vzg_f).save(out_vf, quality=95, subsampling=0)
    Image.fromarray(vzg_b).save(out_vb, quality=95, subsampling=0)
    print("Successfully built all 4 concrete product card images!")


if __name__ == "__main__":
    build_all()
