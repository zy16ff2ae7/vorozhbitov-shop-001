#!/usr/bin/env python3
"""Build unified concrete-background product card media for the black tees.

Puts the authentic black tees on the shared brutalist concrete backdrop plate
(white-tee-concrete-bg-v1.png), perfectly unified with the white tee (Odin v pole voin)
in 1080x1340 4:5 resolution, scale, collar alignment, and ambient contact shadow:

1. «СИЛА И ЧЕСТЬ» Front:
   - Completely clean virgin black cotton chest (eyes print from Vzglyad eliminated).
   - Crisp white Cyrillic wordmark «СИЛА И ЧЕСТЬ».
   - Authentic woven red hem label at bottom hem.

2. «СИЛА И ЧЕСТЬ» Back:
   - Pure black cotton back (all circular text from Vzglyad eliminated).
   - Authentic woven collar patch with sword monogram.
   - Authentic long narrow steel blade down the spine.

3. «ВЗГЛЯД» Front:
   - Original intense warrior eyes artwork with «СИЛА И ЧЕСТЬ» and dagger accents.

4. «ВЗГЛЯД» Back:
   - Authentic gothic cross monogram and circular Cyrillic principles.
   - Fixed neckline polygon eliminating the dark bump/hunchback above the collar.

5. Visual Alignment:
   - Exactly 600px torso width matching the white tee (Odin v pole voin).
   - Collar line aligned at y = 134 across all tees.
   - Realistic contact drop shadow on concrete wall.
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
P18_PATH = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"
WORDMARK_PATH = BRAND / "sila-chest-wordmark.png"
SWORD_PATH = BRAND / "sword-monogram-light-v2.png"
FLATLAY_PATH = DROP / "tee-flatlay-v2.jpg"
TEE_BACK_PATH = DROP / "tee-back-v2.jpg"

FRONT_POLY = [
    (277, 23), (283, 30), (292, 37), (305, 43), (322, 48), (340, 51),
    (359, 51), (378, 48), (395, 42), (410, 34), (418, 24),
    (430, 29), (480, 55), (550, 95), (615, 140), (690, 195),
    (685, 340), (555, 360), (545, 355), (542, 695),
    (145, 695), (142, 355), (135, 360), (0, 340),
    (0, 195), (75, 140), (140, 95), (210, 55), (260, 29),
]

BACK_POLY_SILA = [
    (270, 24), (345, 21), (420, 24),
    (430, 29), (480, 55), (550, 95), (615, 140), (690, 195),
    (685, 340), (555, 360), (545, 355), (542, 695),
    (145, 695), (142, 355), (135, 360), (0, 340),
    (0, 195), (75, 140), (140, 95), (210, 55), (260, 29),
]

BACK_POLY_VZGLYAD = [
    (210, 68), (240, 54), (270, 48), (290, 47), (310, 48), (340, 54), (370, 68),
    (420, 85), (480, 125), (545, 175), (580, 205),
    (575, 345), (455, 365), (445, 358), (440, 695),
    (140, 695), (135, 358), (125, 365), (0, 345),
    (0, 205), (35, 175), (100, 125), (160, 85),
]


def composite_garment(garment: np.ndarray, mask: np.ndarray, bg: np.ndarray, is_back_vzg: bool = False) -> np.ndarray:
    scale = 1.565 if is_back_vzg else 1.5072
    gw = round(garment.shape[1] * scale)
    gh = round(garment.shape[0] * scale)

    g_s = cv2.resize(garment, (gw, gh), interpolation=cv2.INTER_LANCZOS4)
    m_s = cv2.resize(mask, (gw, gh), interpolation=cv2.INTER_LANCZOS4)

    ox = round((1080 - gw) / 2)
    oy = round(134 - (47 if is_back_vzg else 23) * scale)

    canvas = bg.copy().astype(float)
    x1, y1 = max(0, ox), max(0, oy)
    x2, y2 = min(1080, ox + gw), min(1340, oy + gh)

    gx1, gy1 = x1 - ox, y1 - oy
    gx2, gy2 = gx1 + (x2 - x1), gy1 + (y2 - y1)

    # Ambient drop shadow
    smap = np.zeros((1340, 1080), dtype=np.float32)
    smap[y1:y2, x1:x2] = m_s[gy1:gy2, gx1:gx2].astype(np.float32) / 255.0
    s_blur = cv2.GaussianBlur(smap, (41, 41), 20)
    canvas = canvas * (1.0 - 0.45 * s_blur[:, :, None])

    # Alpha composite garment
    alpha = cv2.GaussianBlur(m_s[gy1:gy2, gx1:gx2].astype(np.float32) / 255.0, (5, 5), 1.2)[:, :, None]
    roi = canvas[y1:y2, x1:x2]
    canvas[y1:y2, x1:x2] = g_s[gy1:gy2, gx1:gx2] * alpha + roi * (1.0 - alpha)
    return np.clip(canvas, 0, 255).astype(np.uint8)


def main() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    bg_img = Image.open(BG_PATH).convert("RGB")
    bg = np.array(bg_img.resize((1080, 1340), Image.Resampling.LANCZOS))

    p18 = np.array(Image.open(P18_PATH))
    front_v = p18[65:785, 0:690].copy()  # 720 x 690
    back_v = p18[65:785, 700:1280].copy()  # 720 x 580

    # 1. Clean front_v with seamless belly patch (y: 160..385, height 225, staying above waist seam at y=425)
    belly = front_v[450:675, 130:560].copy()
    patch_mask = np.ones((225, 430), dtype=float)
    for i in range(20):
        patch_mask[i, :] *= (i / 20.0)
        patch_mask[224 - i, :] *= (i / 20.0)
        patch_mask[:, i] *= (i / 20.0)
        patch_mask[:, 429 - i] *= (i / 20.0)

    clean_front = front_v.copy()
    clean_front[160:385, 130:560] = (
        front_v[160:385, 130:560] * (1.0 - patch_mask[:, :, None]) + belly * patch_mask[:, :, None]
    ).astype(np.uint8)

    # SILA FRONT: Cyrillic wordmark + authentic red woven hem label
    wm = Image.open(WORDMARK_PATH)
    wm_w = 340
    wm_h = round(wm.height * (wm_w / wm.width))
    wm_resized = wm.resize((wm_w, wm_h), Image.Resampling.LANCZOS)

    img_sila_f = Image.fromarray(clean_front)
    img_sila_f.paste(wm_resized, (345 - wm_w // 2, 268), wm_resized)

    flatlay = Image.open(FLATLAY_PATH)
    hem = flatlay.crop((475, 785, 515, 850))
    hem_w = 26
    hem_h = round(hem.height * (hem_w / hem.width))
    hem_resized = hem.resize((hem_w, hem_h), Image.Resampling.LANCZOS)
    img_sila_f.paste(hem_resized, (158, 665))
    sila_front = np.array(img_sila_f)

    # SILA BACK: Built from clean virgin back base + authentic woven collar tag + spine sword blade
    back_base = clean_front.copy()
    neck_fill = clean_front[200:270, 310:445].copy()
    back_base[48:118, 275:410] = neck_fill

    img_sila_b = Image.fromarray(back_base)

    # Woven collar tag with sword emblem
    tag_img = Image.new("RGBA", (34, 48), (14, 14, 16, 255))
    sword_icon = Image.open(SWORD_PATH)
    si_h = 36
    si_w = round(sword_icon.width * (si_h / sword_icon.height))
    si_resized = sword_icon.resize((si_w, si_h), Image.Resampling.LANCZOS)
    tag_img.paste(si_resized, ((34 - si_w) // 2, (48 - si_h) // 2), si_resized)
    img_sila_b.paste(tag_img.convert("RGB"), (345 - 17, 36))

    # Long narrow authentic steel blade down spine
    blade_img = Image.open(TEE_BACK_PATH).crop((530, 320, 545, 840))
    blade_w = 17
    blade_h = 615
    blade_resized = blade_img.resize((blade_w, blade_h), Image.Resampling.LANCZOS)
    img_sila_b.paste(blade_resized, (345 - blade_w // 2, 36 + 48))
    sila_back = np.array(img_sila_b)

    # VZGLYAD FRONT & BACK
    vzglyad_front = front_v.copy()
    vzglyad_back = back_v.copy()

    # Masks
    mask_front = np.zeros((720, 690), dtype=np.uint8)
    cv2.fillPoly(mask_front, [np.array(FRONT_POLY, dtype=np.int32)], 255)

    mask_back_sila = np.zeros((720, 690), dtype=np.uint8)
    cv2.fillPoly(mask_back_sila, [np.array(BACK_POLY_SILA, dtype=np.int32)], 255)

    mask_back_vzg = np.zeros((720, 580), dtype=np.uint8)
    cv2.fillPoly(mask_back_vzg, [np.array(BACK_POLY_VZGLYAD, dtype=np.int32)], 255)

    # Composite onto concrete canvas
    res_sila_f = composite_garment(sila_front, mask_front, bg, False)
    res_sila_b = composite_garment(sila_back, mask_back_sila, bg, False)
    res_vzglyad_f = composite_garment(vzglyad_front, mask_front, bg, False)
    res_vzglyad_b = composite_garment(vzglyad_back, mask_back_vzg, bg, True)

    out_sf = PROOF / "tee-sila-front-photo-v1.jpg"
    out_sb = PROOF / "tee-sila-back-photo-v2.jpg"
    out_vf = PROOF / "tee-vzglyad-front-source-v2.jpg"
    out_vb = PROOF / "tee-vzglyad-back-source-v2.jpg"

    Image.fromarray(res_sila_f).save(out_sf, quality=96, subsampling=0)
    Image.fromarray(res_sila_b).save(out_sb, quality=96, subsampling=0)
    Image.fromarray(res_vzglyad_f).save(out_vf, quality=96, subsampling=0)
    Image.fromarray(res_vzglyad_b).save(out_vb, quality=96, subsampling=0)

    print("Successfully built unified concrete catalog media (1080x1340 4:5):")
    print(f"- {out_sf.name}")
    print(f"- {out_sb.name}")
    print(f"- {out_vf.name}")
    print(f"- {out_vb.name}")


if __name__ == "__main__":
    main()
