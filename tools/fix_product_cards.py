#!/usr/bin/env python3
"""Fix all visual defects, blur boxes, wrong prints, and wrong tags in product cards."""
from pathlib import Path
from PIL import Image
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"
SPIN = MINIAPP / "assets/spin"
DROP = MINIAPP / "assets/drop"
PHOTOS = ROOT / "task_materials/chat_export/photos"


def fix_tee_sich():
    print("Fixing tee-sila-i-chest...")
    # 1. Flatlay: clean chest from original flatlay, real folded woven hem tag, blade & collar patch on back
    v1 = Image.open(DROP / "tee-flatlay.jpg").resize((1080, 1080), Image.Resampling.LANCZOS)
    v2 = Image.open(DROP / "tee-flatlay-v2.jpg")
    flatlay = v1.copy()
    # Right shirt (back) with collar patch & blade
    right_shirt = v2.crop((530, 0, 1080, 1080))
    flatlay.paste(right_shirt, (530, 0))
    # Hem label on left shirt
    hem_patch = v2.crop((475, 785, 515, 850))
    flatlay.paste(hem_patch, (475, 785))
    flatlay.save(DROP / "tee-flatlay-v2.jpg", quality=96)
    print("  -> Updated tee-flatlay-v2.jpg")

    # Woven tag patch for 3D spin frames
    tag_patch_front = hem_patch.resize((26, 42), Image.Resampling.LANCZOS)

    # 2. Spin frames tee-sich-01-v4 to 08-v4
    # Frame 01 (front): sharp clean chest without inpainting blur, authentic woven folded label tag
    s1 = Image.open(SPIN / "tee-sich-01.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    s1.paste(tag_patch_front, (633, 475))
    s1.save(SPIN / "tee-sich-01-v4.jpg", quality=96)

    # Frame 02 (angle)
    s2 = Image.open(SPIN / "tee-sich-02.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    tag_patch_02 = hem_patch.resize((20, 40), Image.Resampling.LANCZOS)
    s2.paste(tag_patch_02, (649, 454))
    s2.save(SPIN / "tee-sich-02-v4.jpg", quality=96)

    # Frame 03 (side)
    s3 = Image.open(SPIN / "tee-sich-03.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    tag_patch_03 = hem_patch.resize((22, 40), Image.Resampling.LANCZOS)
    s3.paste(tag_patch_03, (594, 480))
    s3.save(SPIN / "tee-sich-03-v4.jpg", quality=96)

    # Frame 07 (side)
    s7 = Image.open(SPIN / "tee-sich-07.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    tag_patch_07 = hem_patch.resize((22, 40), Image.Resampling.LANCZOS)
    s7.paste(tag_patch_07, (480, 475))
    s7.save(SPIN / "tee-sich-07-v4.jpg", quality=96)

    # Frame 08 (angle)
    s8 = Image.open(SPIN / "tee-sich-08.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    tag_patch_08 = hem_patch.resize((26, 42), Image.Resampling.LANCZOS)
    s8.paste(tag_patch_08, (562, 475))
    s8.save(SPIN / "tee-sich-08-v4.jpg", quality=96)

    # Frames 04, 05, 06 are back views with collar patch and blade; they are preserved.
    print("  -> Updated tee-sich spin frames v4")


def fix_tee_vzglyad():
    print("Fixing tee-vzglyad...")
    p18_path = PHOTOS / "photo_18@24-10-2026_18-41-33.jpg"
    p18 = Image.open(p18_path)

    # 1. Detail image (1080x720) macro photo of the authentic warrior eyes print
    crop_detail = p18.crop((85, 200, 625, 560)).resize((1080, 720), Image.Resampling.LANCZOS)
    crop_detail.save(DROP / "tee-vzglyad-detail-v3.jpg", quality=96)
    print("  -> Updated tee-vzglyad-detail-v3.jpg")

    # 2. Extract transparent front graphic from photo 18
    im_front = p18.crop((186, 246, 526, 504)).convert("RGB")
    arr_f = np.array(im_front, dtype=np.float32)
    h, w, _ = arr_f.shape
    alpha_f = np.ones((h, w), dtype=np.float32)
    max_f = np.max(arr_f[105:, :], axis=2)
    alpha_text = np.clip((max_f - 24.0) / 40.0, 0.0, 1.0)
    alpha_text = cv2.GaussianBlur(alpha_text, (3, 3), 0.5)
    alpha_f[105:, :] = alpha_text
    rgba_front = np.dstack([arr_f.astype(np.uint8), (alpha_f * 255).astype(np.uint8)])
    img_rgba_front = Image.fromarray(rgba_front, "RGBA")

    # Scale front art for 3D spin frames
    front_scale = 210.0 / img_rgba_front.width
    front_size = (int(img_rgba_front.width * front_scale), int(img_rgba_front.height * front_scale))
    front_scaled = img_rgba_front.resize(front_size, Image.Resampling.LANCZOS)

    # Clean chest frame from tee-sich-01.jpg
    s1_cv = cv2.imread(str(SPIN / "tee-sich-01.jpg"))
    gray = cv2.cvtColor(s1_cv, cv2.COLOR_BGR2GRAY)
    mask = np.zeros_like(gray)
    mask[160:240, 420:660] = (gray[160:240, 420:660] > 50).astype(np.uint8) * 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    clean_chest_s1 = cv2.inpaint(s1_cv, mask, 3, cv2.INPAINT_TELEA)
    clean_base = Image.fromarray(cv2.cvtColor(clean_chest_s1, cv2.COLOR_BGR2RGB)).resize((1080, 590), Image.Resampling.LANCZOS).convert("RGBA")

    # Frame 01 (front)
    v01 = clean_base.copy()
    v01.alpha_composite(front_scaled, (540 - front_size[0] // 2, 150))
    v01.convert("RGB").save(SPIN / "tee-vzglyad-01-v3.jpg", quality=96)
    print("  -> Updated tee-vzglyad-01-v3.jpg")

    # Frame 02 (angle front)
    v02_art = img_rgba_front.resize((int(front_size[0] * 0.9), front_size[1]), Image.Resampling.LANCZOS)
    v02 = Image.open(SPIN / "tee-vzglyad-02.jpg").resize((1080, 590), Image.Resampling.LANCZOS).convert("RGBA")
    v02_cv = cv2.cvtColor(np.array(v02), cv2.COLOR_RGBA2BGR)
    mask2 = np.zeros(v02_cv.shape[:2], np.uint8)
    mask2[150:310, 480:680] = 255
    v02_clean = cv2.inpaint(v02_cv, mask2, 5, cv2.INPAINT_TELEA)
    v02 = Image.fromarray(cv2.cvtColor(v02_clean, cv2.COLOR_BGR2RGBA))
    v02.alpha_composite(v02_art, (510, 150))
    v02.convert("RGB").save(SPIN / "tee-vzglyad-02-v3.jpg", quality=96)

    # Frame 08 (angle front other side)
    v08 = Image.open(SPIN / "tee-vzglyad-08.jpg").resize((1080, 590), Image.Resampling.LANCZOS).convert("RGBA")
    v08_cv = cv2.cvtColor(np.array(v08), cv2.COLOR_RGBA2BGR)
    mask8 = np.zeros(v08_cv.shape[:2], np.uint8)
    mask8[150:310, 420:600] = 255
    v08_clean = cv2.inpaint(v08_cv, mask8, 5, cv2.INPAINT_TELEA)
    v08 = Image.fromarray(cv2.cvtColor(v08_clean, cv2.COLOR_BGR2RGBA))
    v08.alpha_composite(v02_art, (430, 150))
    v08.convert("RGB").save(SPIN / "tee-vzglyad-08-v3.jpg", quality=96)

    # Frame 07 (side) - restore unblemished original to remove fake hem stick monogram
    v07_orig = Image.open(SPIN / "tee-vzglyad-07.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    v07_orig.save(SPIN / "tee-vzglyad-07-v3.jpg", quality=96)

    # 3. Back frames: use uncorrupted originals with authentic stone cross & circular values ring
    v05_orig = Image.open(SPIN / "tee-vzglyad-05.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    v05_orig.save(SPIN / "tee-vzglyad-05-v3.jpg", quality=96)

    v04_orig = Image.open(SPIN / "tee-vzglyad-04.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    v04_orig.save(SPIN / "tee-vzglyad-04-v3.jpg", quality=96)

    v06_orig = Image.open(SPIN / "tee-vzglyad-06.jpg").resize((1080, 590), Image.Resampling.LANCZOS)
    v06_orig.save(SPIN / "tee-vzglyad-06-v3.jpg", quality=96)
    print("  -> Updated tee-vzglyad spin frames v3 (clean front art, clean circular values back, no fake hem monograms)")


def fix_tee_odin():
    print("Fixing tee-odin-v-pole-voin...")
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from refine_white_spin import concrete_plate, garment_cutout, composite
    from PIL import ImageOps, ImageFilter

    src_path = DROP / "odin-v-pole-voin.jpg"
    src = Image.open(src_path)

    # 1. Authentic flatlay back: odin-voin-back-v3.jpg from original photo 7
    back_orig = src.crop((700, 0, 1402, 700)).convert("RGB")
    back_orig.save(DROP / "odin-voin-back-v3.jpg", quality=96, subsampling=0)
    print("  -> Updated odin-voin-back-v3.jpg with authentic gothic print")

    # 2. Authentic board: odin-v-pole-voin-v3.jpg
    v3_board = Image.open(DROP / "odin-v-pole-voin-v3.jpg")
    v3_board.paste(back_orig, (700, 0))
    cell_3 = src.crop((843, 700, 1128, 1013)).convert("RGB")
    v3_board.paste(cell_3, (843, 700))
    v3_board.save(DROP / "odin-v-pole-voin-v3.jpg", quality=96, subsampling=0)
    print("  -> Updated odin-v-pole-voin-v3.jpg with authentic gothic print")

    # 3. Authentic detail: odin-voin-detail-v3.jpg
    detail = Image.new("RGB", (1080, 720), (222, 222, 218))
    detail_cells = [
        (0, 700, 286, 1013),
        (286, 700, 558, 1013),
        (558, 700, 843, 1013),
        (843, 700, 1128, 1013),
    ]
    for index, bounds in enumerate(detail_cells):
        crop = src.crop(bounds).convert("RGB")
        fitted = ImageOps.contain(crop, (536, 356), Image.Resampling.LANCZOS)
        cell = ImageOps.fit(crop, (540, 360), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(12))
        cell.paste(fitted, ((540 - fitted.width) // 2, (360 - fitted.height) // 2))
        detail.paste(cell, ((index % 2) * 540, (index // 2) * 360))
    detail.save(DROP / "odin-voin-detail-v3.jpg", quality=96, subsampling=0)
    print("  -> Updated odin-voin-detail-v3.jpg with authentic gothic calligraphy macro")

    # 4. Spin frames: 04, 05, 06 from original video frames (authentic gothic calligraphy)
    plate = concrete_plate()
    originals = ROOT / "approval/brand-logo-review-2026-10-25-v2/white-sources"
    symmetric_outline = [
        (250, 260), (470, 260), (560, 310), (630, 400),
        (720, 520), (720, 1150), (0, 1150), (0, 520),
        (90, 400), (160, 310),
    ]

    cutout_04 = garment_cutout(originals / "odin-voin-04-original.jpg")
    frame_04 = composite(plate, cutout_04)
    frame_04.save(SPIN / "odin-voin-04-v8.jpg", quality=95, subsampling=0, optimize=True)

    cutout_05 = garment_cutout(originals / "odin-voin-05-original.jpg", allowed_polygon=symmetric_outline)
    frame_05 = composite(
        plate,
        cutout_05,
        width_scale=1.14,
        garment_height=452,
        center_x=535,
    )
    frame_05.save(SPIN / "odin-voin-05-v8.jpg", quality=95, subsampling=0, optimize=True)

    cutout_06 = garment_cutout(originals / "odin-voin-06-original.jpg")
    frame_06 = composite(plate, cutout_06)
    frame_06.save(SPIN / "odin-voin-06-v8.jpg", quality=95, subsampling=0, optimize=True)
    print("  -> Updated odin-voin spin frames 04, 05, 06 with authentic gothic print")


if __name__ == "__main__":
    fix_tee_sich()
    fix_tee_vzglyad()
    fix_tee_odin()
    print("All fixes applied successfully!")
