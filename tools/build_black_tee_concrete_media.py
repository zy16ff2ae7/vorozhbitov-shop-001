#!/usr/bin/env python3
"""Build unified concrete-background product card media for the black tees.

Puts authentic black tees on the shared brutalist concrete backdrop plate
(white-tee-concrete-bg-v1.png), perfectly unified with the white tee (Odin v pole voin)
in 1080x1340 4:5 resolution, scale, collar alignment, and ambient contact shadow:

1. «СИЛА И ЧЕСТЬ» Front:
   - 100% authentic garment from studio flatlay photoshoot (tee-flatlay-v2.jpg).
   - Authentic Cyrillic wordmark «СИЛА И ЧЕСТЬ».
   - Authentic woven brand label on bottom right hem.
   - Natural crewneck collar scoop and complete sleeves.

2. «СИЛА И ЧЕСТЬ» Back:
   - 100% authentic garment from studio flatlay photoshoot (tee-flatlay-v2.jpg).
   - Authentic crewneck collar.
   - Authentic woven collar tag with sword monogram (•В•).
   - Authentic vertical black ribbon down the spine to the hem.
   - Authentic Damascus steel blade with engraved fullers down the spine.

3. «ВЗГЛЯД» Front:
   - Authentic warrior eyes artwork with «СИЛА И ЧЕСТЬ» and dagger accents (photo_18).
   - Inner collar monogram, natural 3D ghost mannequin drape.

4. «ВЗГЛЯД» Back:
   - Authentic gothic cross monogram (photo_18).
   - Complete circular Cyrillic principles (МУЖЕСТВО + СИЛА + ЧЕСТЬ + ДОМ + СЕМЬЯ + ВОЛЯ + ХАРАКТЕР + ВЕРНОСТЬ + ДИСЦИПЛИНА + ДУХ).
   - Clean crewneck collar with zero hunchback or notches.

5. Visual System Alignment:
   - Exact 1080x1340 4:5 aspect ratio.
   - Collar line aligned at y = 134 across all tees.
   - Soft organic alpha matting preserving fabric softness and natural drape.
   - Subtle realistic ambient contact shadow on the brutalist concrete wall.
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

BG_PATH = PROOF / "white-tee-concrete-bg-v1.png"
P18_PATH = ROOT / "task_materials/chat_export/photos/photo_18@24-10-2026_18-41-33.jpg"
FLATLAY_PATH = DROP / "tee-flatlay-v2.jpg"


def composite_to_concrete(
    bg: np.ndarray, garment_bgr: np.ndarray, mask_u8: np.ndarray, target_h: int = 916, oy: int = 134
) -> np.ndarray:
    ys, xs = np.where(mask_u8)
    ymin, ymax = int(ys.min()), int(ys.max())
    xmin, xmax = int(xs.min()), int(xs.max())

    crop_g = garment_bgr[ymin : ymax + 1, xmin : xmax + 1]
    crop_m = mask_u8[ymin : ymax + 1, xmin : xmax + 1]

    scale = target_h / crop_g.shape[0]
    target_w = round(crop_g.shape[1] * scale)

    g_s = cv2.resize(crop_g, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
    m_s = cv2.resize(crop_m.astype(np.float32), (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

    alpha = cv2.GaussianBlur(m_s, (5, 5), 1.0)[:, :, None]

    canvas = bg.copy().astype(np.float32)
    ox = (1080 - target_w) // 2

    # Subtle natural contact shadow (tight and soft)
    s_blur = cv2.GaussianBlur(m_s, (15, 15), 5.0)
    canvas_s = np.zeros((1340, 1080), dtype=np.float32)
    canvas_s[oy : oy + target_h, ox : ox + target_w] = s_blur
    canvas = canvas * (1.0 - 0.20 * canvas_s[:, :, None])

    roi = canvas[oy : oy + target_h, ox : ox + target_w]
    canvas[oy : oy + target_h, ox : ox + target_w] = g_s * alpha + roi * (1.0 - alpha)
    return np.clip(canvas, 0, 255).astype(np.uint8)


def build_sila_media(bg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flatlay = cv2.imread(str(FLATLAY_PATH))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    # 1. SILA BACK: Authentic right half of flatlay (x: 530..1080)
    back_crop = flatlay[:, 530:]
    right_half = back_crop[:, 221:]  # w=329, h=1080 (blade at x=0)
    gray_r = cv2.cvtColor(right_half, cv2.COLOR_BGR2GRAY)

    dark_r = (gray_r < 48).astype(np.uint8)
    dark_r[240:810, :25] = 1  # tag starts at y=240, blade down to y=810
    dark_r = cv2.morphologyEx(dark_r, cv2.MORPH_CLOSE, kernel)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(dark_r, 8)
    clean_r = (labels == labels[400, 100]).astype(np.uint8)

    inv_r = 1 - clean_r
    _, inv_labels, _, _ = cv2.connectedComponentsWithStats(inv_r, 8)
    edge_l = np.unique(
        np.concatenate([inv_labels[0, :], inv_labels[-1, :], inv_labels[:, 0], inv_labels[:, -1]])
    )
    clean_r[~np.isin(inv_labels, edge_l)] = 1
    clean_r[810:, :] = 0
    clean_r[:240, :] = 0

    full_sila_b = np.zeros((1080, 329 * 2, 3), dtype=np.uint8)
    full_mask_b = np.zeros((1080, 329 * 2), dtype=np.uint8)
    full_sila_b[:, 329:] = right_half
    full_mask_b[:, 329:] = clean_r
    full_sila_b[:, :329] = cv2.flip(right_half, 1)
    full_mask_b[:, :329] = cv2.flip(clean_r, 1)

    res_sila_b = composite_to_concrete(bg, full_sila_b, full_mask_b, 916, 134)

    # 2. SILA FRONT: Authentic left half of flatlay (x: 0..530)
    front_crop = flatlay[:, :530].copy()
    mask_f = np.full(front_crop.shape[:2], cv2.GC_BGD, dtype=np.uint8)

    poly_f = np.array(
        [
            (292, 205),
            (200, 215),
            (100, 265),
            (5, 345),
            (5, 460),
            (60, 460),
            (120, 440),
            (120, 835),
            (525, 835),
            (525, 440),
            (530, 430),
            (530, 340),
            (480, 265),
            (380, 215),
        ],
        np.int32,
    )
    cv2.fillPoly(mask_f, [poly_f], cv2.GC_PR_FGD)

    poly_inner = np.array(
        [
            (292, 245),
            (220, 250),
            (130, 300),
            (35, 370),
            (45, 420),
            (135, 400),
            (140, 785),
            (485, 785),
            (485, 420),
            (520, 400),
            (520, 370),
            (440, 300),
            (360, 250),
        ],
        np.int32,
    )
    cv2.fillPoly(mask_f, [poly_inner], cv2.GC_FGD)

    mask_f[380:440, 100:480] = cv2.GC_FGD
    mask_f[720:825, 430:510] = cv2.GC_FGD  # authentic red hem label

    bgd_f = np.zeros((1, 65), np.float64)
    fgd_f = np.zeros((1, 65), np.float64)
    cv2.grabCut(front_crop, mask_f, None, bgd_f, fgd_f, 5, cv2.GC_INIT_WITH_MASK)
    clean_f = np.where((mask_f == cv2.GC_FGD) | (mask_f == cv2.GC_PR_FGD), 1, 0).astype(np.uint8)

    full_sila_f = np.zeros((1080, 292 + 282, 3), dtype=np.uint8)
    full_mask_f = np.zeros((1080, 292 + 282), dtype=np.uint8)
    full_sila_f[:, :530] = front_crop
    full_mask_f[:, :530] = clean_f

    left_cuff = front_crop[:, 10:54]
    left_cuff_m = clean_f[:, 10:54]
    full_sila_f[:, 530:] = cv2.flip(left_cuff, 1)
    full_mask_f[:, 530:] = cv2.flip(left_cuff_m, 1)

    res_sila_f = composite_to_concrete(bg, full_sila_f, full_mask_f, 916, 134)
    return res_sila_f, res_sila_b


def build_vzglyad_media(bg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p18 = cv2.imread(str(P18_PATH))

    # 1. VZGLYAD BACK: Authentic right half of photo_18
    back_v = p18[:, 660:].copy()  # h: 853, w: 620
    outer_poly_b = np.array(
        [
            (305, 75),
            (240, 85),
            (170, 115),
            (90, 175),
            (0, 290),
            (0, 340),
            (0, 480),
            (50, 490),
            (95, 485),
            (95, 790),
            (490, 790),
            (490, 485),
            (560, 490),
            (620, 480),
            (620, 340),
            (615, 290),
            (520, 175),
            (440, 115),
            (370, 85),
        ],
        np.int32,
    )
    mask_vb = np.full(back_v.shape[:2], cv2.GC_BGD, dtype=np.uint8)
    cv2.fillPoly(mask_vb, [outer_poly_b], cv2.GC_PR_FGD)

    inner_poly_b = np.array(
        [
            (305, 115),
            (250, 125),
            (190, 150),
            (120, 200),
            (40, 330),
            (50, 440),
            (115, 460),
            (115, 760),
            (475, 760),
            (475, 460),
            (560, 440),
            (570, 330),
            (490, 200),
            (420, 150),
            (360, 125),
        ],
        np.int32,
    )
    cv2.fillPoly(mask_vb, [inner_poly_b], cv2.GC_FGD)

    bgd_b = np.zeros((1, 65), np.float64)
    fgd_b = np.zeros((1, 65), np.float64)
    cv2.grabCut(back_v, mask_vb, None, bgd_b, fgd_b, 5, cv2.GC_INIT_WITH_MASK)
    final_mask_vb = np.where((mask_vb == cv2.GC_FGD) | (mask_vb == cv2.GC_PR_FGD), 1, 0).astype(np.uint8)
    poly_mask_b = np.zeros(back_v.shape[:2], np.uint8)
    cv2.fillPoly(poly_mask_b, [outer_poly_b], 1)
    final_mask_vb = final_mask_vb & poly_mask_b

    res_vzg_b = composite_to_concrete(bg, back_v, final_mask_vb, 916, 134)

    # 2. VZGLYAD FRONT: Authentic left half of photo_18
    front_v = p18[:, :660].copy()  # h: 853, w: 660
    outer_poly_f = np.array(
        [
            (340, 75),
            (270, 85),
            (200, 115),
            (120, 175),
            (5, 290),
            (0, 340),
            (0, 480),
            (50, 490),
            (105, 485),
            (105, 790),
            (535, 790),
            (535, 485),
            (600, 490),
            (660, 480),
            (660, 340),
            (655, 290),
            (560, 175),
            (480, 115),
            (410, 85),
        ],
        np.int32,
    )
    mask_vf = np.full(front_v.shape[:2], cv2.GC_BGD, dtype=np.uint8)
    cv2.fillPoly(mask_vf, [outer_poly_f], cv2.GC_PR_FGD)

    inner_poly_f = np.array(
        [
            (340, 115),
            (280, 125),
            (220, 150),
            (150, 200),
            (40, 330),
            (50, 440),
            (125, 460),
            (125, 760),
            (515, 760),
            (515, 460),
            (600, 440),
            (610, 330),
            (530, 200),
            (460, 150),
            (400, 125),
        ],
        np.int32,
    )
    cv2.fillPoly(mask_vf, [inner_poly_f], cv2.GC_FGD)

    bgd_f = np.zeros((1, 65), np.float64)
    fgd_f = np.zeros((1, 65), np.float64)
    cv2.grabCut(front_v, mask_vf, None, bgd_f, fgd_f, 5, cv2.GC_INIT_WITH_MASK)
    final_mask_vf = np.where((mask_vf == cv2.GC_FGD) | (mask_vf == cv2.GC_PR_FGD), 1, 0).astype(np.uint8)
    poly_mask_f = np.zeros(front_v.shape[:2], np.uint8)
    cv2.fillPoly(poly_mask_f, [outer_poly_f], 1)
    final_mask_vf = final_mask_vf & poly_mask_f

    res_vzg_f = composite_to_concrete(bg, front_v, final_mask_vf, 916, 134)
    return res_vzg_f, res_vzg_b


def main() -> None:
    PROOF.mkdir(parents=True, exist_ok=True)
    bg_img = Image.open(BG_PATH).convert("RGB")
    bg = np.array(bg_img.resize((1080, 1340), Image.Resampling.LANCZOS))

    print("Building authentic Sila media on concrete backdrop...")
    res_sila_f, res_sila_b = build_sila_media(bg)

    print("Building authentic Vzglyad media on concrete backdrop...")
    res_vzg_f, res_vzg_b = build_vzglyad_media(bg)

    out_sf = PROOF / "tee-sila-front-photo-v1.jpg"
    out_sb = PROOF / "tee-sila-back-photo-v2.jpg"
    out_vf = PROOF / "tee-vzglyad-front-source-v2.jpg"
    out_vb = PROOF / "tee-vzglyad-back-source-v2.jpg"

    Image.fromarray(res_sila_f).save(out_sf, quality=96, subsampling=0)
    Image.fromarray(res_sila_b).save(out_sb, quality=96, subsampling=0)
    Image.fromarray(res_vzg_f).save(out_vf, quality=96, subsampling=0)
    Image.fromarray(res_vzg_b).save(out_vb, quality=96, subsampling=0)

    print("Successfully built authentic unified concrete catalog media (1080x1340 4:5):")
    print(f"- {out_sf}")
    print(f"- {out_sb}")
    print(f"- {out_vf}")
    print(f"- {out_vb}")


if __name__ == "__main__":
    main()
