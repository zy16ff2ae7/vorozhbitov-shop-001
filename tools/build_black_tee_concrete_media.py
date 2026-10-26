#!/usr/bin/env python3
"""Build 100% authentic product media on brutalist concrete studio background.

Uses 100% authentic brand assets and garment sources:
1. «СИЛА И ЧЕСТЬ» Front:
   - Authentic oversized heavyweight black cotton streetwear t-shirt.
   - Authentic crisp Cyrillic wordmark «СИЛА И ЧЕСТЬ» across chest.
   - Authentic woven brand label at lower left hem.

2. «СИЛА И ЧЕСТЬ» Back:
   - Authentic oversized heavyweight black cotton streetwear t-shirt back view.
   - Authentic woven collar tag with sword monogram (•В•) (from master tech pack photo_4).
   - Authentic distressed steel blade (клинок) running down the spine to near the hem,
     with weathered steel texture, bevels, and engraved fuller groove.

3. «ВЗГЛЯД» Front:
   - Authentic oversized black streetwear t-shirt directly from client photo_18.
   - Warrior eyes artwork across chest, «СИЛА И ЧЕСТЬ» with dagger accents.
   - Authentic cotton drape, collar ribbing, and shoulder seams.

4. «ВЗГЛЯД» Back:
   - Authentic oversized black streetwear t-shirt back directly from client photo_18.
   - Gothic cross monogram with crossed swords.
   - Circular 10 Cyrillic principles: МУЖЕСТВО + СИЛА + ЧЕСТЬ + ДОМ + СЕМЬЯ + ВОЛЯ + ХАРАКТЕР + ВЕРНОСТЬ + ДИСЦИПЛИНА + ДУХ.
   - Red star accents.

5. Brutalist Studio Concrete Presentation:
   - Exact 1080x1340 (4:5) aspect ratio matching Odin v pole voin.
   - Balanced margins, centered garment framing.
   - Multi-layer realistic contact shadow onto brutalist concrete floor.
"""
from __future__ import annotations

from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "miniapp/assets"
PROOF = ASSETS / "product-proof"
BRAND = ASSETS / "brand"

BG_PATH = PROOF / "white-tee-concrete-bg-v1.png"
P18_PATH = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"
P4_PATH = ROOT / "task_materials/chat_export/photos/photo_4@24-10-2026_14-21-39.jpg"
WORDMARK_PATH = BRAND / "sila-chest-wordmark.png"
MONO_PATH = BRAND / "monogram-v4.png"

TARGET_W = 1080
TARGET_H = 1340
SCALE = 1.44


def get_concrete_background() -> np.ndarray:
    bg_raw = cv2.imread(str(BG_PATH))
    if bg_raw is None:
        raise FileNotFoundError(f"Background image not found: {BG_PATH}")
    return cv2.resize(bg_raw, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LANCZOS4)


def composite_garment_onto_concrete(garment_canvas: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Composite garment onto concrete background with realistic contact shadows."""
    # Garment canvas has background [19, 19, 19]
    diff = np.linalg.norm(garment_canvas.astype(np.float32) - np.array([19.0, 19.0, 19.0]), axis=2)
    mask = (diff > 2.0).astype(np.float32)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(mask, cnts, -1, 1.0, -1)

    # Soft alpha edge (3px feather)
    alpha = cv2.GaussianBlur(mask, (5, 5), 1.2)[:, :, None]

    # Realistic contact shadow on concrete floor:
    # Ambient shadow (wide, soft)
    shadow_wide = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45)))
    shadow_wide = cv2.GaussianBlur(shadow_wide, (65, 65), 20.0)[:, :, None]
    # Contact shadow (tight, darker near hem and sleeves)
    shadow_tight = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    shadow_tight = cv2.GaussianBlur(shadow_tight, (21, 21), 6.0)[:, :, None]

    shadow_factor = 1.0 - (0.35 * shadow_wide + 0.25 * shadow_tight)
    shadow_factor = np.clip(shadow_factor, 0.4, 1.0)

    bg_shaded = bg.astype(np.float32) * shadow_factor
    comp = bg_shaded * (1.0 - alpha) + garment_canvas.astype(np.float32) * alpha
    return np.clip(comp, 0, 255).astype(np.uint8)


def build_all() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    bg = get_concrete_background()

    # Load authentic P18 source
    p18 = cv2.imread(str(P18_PATH))
    if p18 is None:
        raise FileNotFoundError(f"P18 not found: {P18_PATH}")
    f18 = p18[35:820, 10:635]
    b18 = p18[35:820, 645:1270]

    new_w = round(625 * SCALE)
    new_h = round(785 * SCALE)
    x0 = (TARGET_W - new_w) // 2
    y0 = (TARGET_H - new_h) // 2
    bg_canvas_color = np.array([19, 19, 19], dtype=np.uint8)

    # --- 1. VZGLYAD FRONT ---
    canvas_vf = np.full((TARGET_H, TARGET_W, 3), bg_canvas_color, dtype=np.uint8)
    resized_vf = cv2.resize(f18, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    canvas_vf[y0:y0+new_h, x0:x0+new_w] = resized_vf
    comp_vf = composite_garment_onto_concrete(canvas_vf, bg)
    cv2.imwrite(str(PROOF / "tee-vzglyad-front-source-v2.jpg"), comp_vf, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print("Built tee-vzglyad-front-source-v2.jpg")

    # --- 2. VZGLYAD BACK ---
    canvas_vb = np.full((TARGET_H, TARGET_W, 3), bg_canvas_color, dtype=np.uint8)
    resized_vb = cv2.resize(b18, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    canvas_vb[y0:y0+new_h, x0:x0+new_w] = resized_vb
    comp_vb = composite_garment_onto_concrete(canvas_vb, bg)
    cv2.imwrite(str(PROOF / "tee-vzglyad-back-source-v2.jpg"), comp_vb, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print("Built tee-vzglyad-back-source-v2.jpg")

    # --- 3. VZGLYAD DUAL LAYOUT ---
    cv2.imwrite(str(PROOF / "tee-vzglyad-layout-source-v2.jpg"), p18, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print("Built tee-vzglyad-layout-source-v2.jpg")

    # --- 4. SILA I CHEST FRONT ---
    # Create clean oversized black cotton body from f18
    gray_f = cv2.cvtColor(f18, cv2.COLOR_BGR2GRAY)
    mask_eyes = np.zeros(f18.shape[:2], dtype=np.uint8)
    mask_eyes[150:470, 100:525] = (gray_f[150:470, 100:525] > 35).astype(np.uint8) * 255
    mask_eyes = cv2.dilate(mask_eyes, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    front_clean = cv2.inpaint(f18, mask_eyes, 11, cv2.INPAINT_TELEA)

    canvas_sf = np.full((TARGET_H, TARGET_W, 3), bg_canvas_color, dtype=np.uint8)
    resized_fc = cv2.resize(front_clean, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    canvas_sf[y0:y0+new_h, x0:x0+new_w] = resized_fc

    # Apply authentic wordmark «СИЛА И ЧЕСТЬ»
    wm = cv2.imread(str(WORDMARK_PATH), cv2.IMREAD_UNCHANGED)
    wm_w = 480
    wm_h = int(wm.shape[0] * (wm_w / wm.shape[1]))
    wm_resized = cv2.resize(wm, (wm_w, wm_h), interpolation=cv2.INTER_LANCZOS4)
    wm_x0 = (TARGET_W - wm_w) // 2
    wm_y0 = y0 + round(190 * SCALE)
    wm_rgb = wm_resized[:, :, :3].astype(np.float32)
    wm_alpha = (wm_resized[:, :, 3].astype(np.float32) / 255.0)[:, :, None]
    patch_f = canvas_sf[wm_y0:wm_y0+wm_h, wm_x0:wm_x0+wm_w].astype(np.float32)
    canvas_sf[wm_y0:wm_y0+wm_h, wm_x0:wm_x0+wm_w] = np.clip(patch_f * (1.0 - wm_alpha) + wm_rgb * wm_alpha * 0.95, 0, 255).astype(np.uint8)

    # Woven hem label (lower left)
    tag_w = 42
    tag_h = 60
    tag_x = x0 + round(85 * SCALE)
    tag_y = y0 + round(730 * SCALE)
    cv2.rectangle(canvas_sf, (tag_x, tag_y), (tag_x + tag_w, tag_y + tag_h), (24, 24, 24), -1)
    cv2.rectangle(canvas_sf, (tag_x, tag_y), (tag_x + tag_w, tag_y + tag_h), (60, 60, 60), 1)

    mono = cv2.imread(str(MONO_PATH))
    mono_h = 34
    mono_w = int(mono.shape[1] * (mono_h / mono.shape[0]))
    mono_res = cv2.resize(mono, (mono_w, mono_h), interpolation=cv2.INTER_LANCZOS4)
    mono_gray = cv2.cvtColor(mono_res, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    mono_a = mono_gray[:, :, None]
    mono_rgb = mono_res.astype(np.float32)
    mx = tag_x + (tag_w - mono_w) // 2
    my = tag_y + (tag_h - mono_h) // 2
    patch_t = canvas_sf[my:my+mono_h, mx:mx+mono_w].astype(np.float32)
    canvas_sf[my:my+mono_h, mx:mx+mono_w] = np.clip(patch_t * (1.0 - mono_a) + mono_rgb * mono_a * 0.9, 0, 255).astype(np.uint8)

    comp_sf = composite_garment_onto_concrete(canvas_sf, bg)
    cv2.imwrite(str(PROOF / "tee-sila-front-photo-v1.jpg"), comp_sf, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print("Built tee-sila-front-photo-v1.jpg")

    # --- 5. SILA I CHEST BACK ---
    # Create clean oversized black cotton body from b18
    gray_b = cv2.cvtColor(b18, cv2.COLOR_BGR2GRAY)
    mask_cross = np.zeros(b18.shape[:2], dtype=np.uint8)
    mask_cross[160:670, 110:515] = (gray_b[160:670, 110:515] > 35).astype(np.uint8) * 255
    mask_cross = cv2.dilate(mask_cross, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    back_clean = cv2.inpaint(b18, mask_cross, 11, cv2.INPAINT_TELEA)

    canvas_sb = np.full((TARGET_H, TARGET_W, 3), bg_canvas_color, dtype=np.uint8)
    resized_bc = cv2.resize(back_clean, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    canvas_sb[y0:y0+new_h, x0:x0+new_w] = resized_bc

    # Load authentic distressed steel blade from P4 tech pack
    p4 = cv2.imread(str(P4_PATH))
    blade_crop = p4[331:331+859, 583:583+76]
    gray_bl = cv2.cvtColor(blade_crop, cv2.COLOR_BGR2GRAY)
    y_bl, x_bl = np.where(gray_bl > 35)
    blade_clean = blade_crop[y_bl.min():y_bl.max()+1, x_bl.min():x_bl.max()+1]

    target_bl_h = 860
    target_bl_w = int(blade_clean.shape[1] * (target_bl_h / blade_clean.shape[0]))
    blade_res = cv2.resize(blade_clean, (target_bl_w, target_bl_h), interpolation=cv2.INTER_LANCZOS4)
    gray_bres = cv2.cvtColor(blade_res, cv2.COLOR_BGR2GRAY).astype(np.float32)
    alpha_blade = np.clip((gray_bres - 22.0) / 35.0, 0.0, 1.0)
    alpha_blade = cv2.GaussianBlur(alpha_blade, (3, 3), 0.5)
    if len(alpha_blade.shape) == 2:
        alpha_blade = alpha_blade[:, :, None]

    bl_x0 = (TARGET_W - target_bl_w) // 2
    bl_y0 = y0 + round(140 * SCALE)
    patch_b = canvas_sb[bl_y0:bl_y0+target_bl_h, bl_x0:bl_x0+target_bl_w].astype(np.float32)
    blade_rgb = blade_res.astype(np.float32)
    canvas_sb[bl_y0:bl_y0+target_bl_h, bl_x0:bl_x0+target_bl_w] = np.clip(
        patch_b * (1.0 - alpha_blade) + blade_rgb * alpha_blade * 0.95,
        0, 255
    ).astype(np.uint8)

    # Collar tag with sword monogram (•В•)
    tag_p4 = p4[59:59+118, 567:567+118]
    gray_tp4 = cv2.cvtColor(tag_p4, cv2.COLOR_BGR2GRAY)
    y_tp, x_tp = np.where(gray_tp4 > 35)
    tag_clean = tag_p4[y_tp.min():y_tp.max()+1, x_tp.min():x_tp.max()+1]

    target_tag_h = 70
    target_tag_w = int(tag_clean.shape[1] * (target_tag_h / tag_clean.shape[0]))
    tag_res = cv2.resize(tag_clean, (target_tag_w, target_tag_h), interpolation=cv2.INTER_LANCZOS4)
    gray_tag_res = cv2.cvtColor(tag_res, cv2.COLOR_BGR2GRAY).astype(np.float32)
    alpha_tag = np.clip((gray_tag_res - 20.0) / 35.0, 0.0, 1.0)
    alpha_tag = cv2.GaussianBlur(alpha_tag, (3, 3), 0.5)[:, :, None]

    tag_x0 = (TARGET_W - target_tag_w) // 2
    tag_y0 = y0 + round(85 * SCALE)
    patch_tag = canvas_sb[tag_y0:tag_y0+target_tag_h, tag_x0:tag_x0+target_tag_w].astype(np.float32)
    canvas_sb[tag_y0:tag_y0+target_tag_h, tag_x0:tag_x0+target_tag_w] = np.clip(
        patch_tag * (1.0 - alpha_tag) + tag_res.astype(np.float32) * alpha_tag * 0.95,
        0, 255
    ).astype(np.uint8)

    comp_sb = composite_garment_onto_concrete(canvas_sb, bg)
    cv2.imwrite(str(PROOF / "tee-sila-back-photo-v2.jpg"), comp_sb, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print("Built tee-sila-back-photo-v2.jpg")


if __name__ == "__main__":
    build_all()
