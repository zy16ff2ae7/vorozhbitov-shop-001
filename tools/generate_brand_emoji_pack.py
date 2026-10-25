import os
import math
import zipfile
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import numpy as np

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK_DIRS = [
    os.path.join(REPO_DIR, "custom_emoji_pack"),
    os.path.join(REPO_DIR, "custom_emoji_pack_v3"),
]
for pdir in PACK_DIRS:
    os.makedirs(os.path.join(pdir, "icons"), exist_ok=True)

# High-resolution supersampling canvas (600x600 -> 100x100 via Lanczos)
S = 600

# Color palette: Authentic "СИЛА И ЧЕСТЬ" tactical streetwear
C_WHITE = (255, 255, 255, 255)
C_RED = (225, 6, 0, 255)            # Tactical crimson accent #E10600
C_CARBON = (13, 16, 20, 255)         # Matte streetwear cotton / carbon black
C_DARK = (20, 25, 32, 255)           # Armor plate dark
C_STEEL_HI = (225, 235, 245, 255)    # Cold brushed steel highlight
C_STEEL_MID = (165, 180, 195, 255)   # Steel midtone
C_STEEL_LO = (85, 100, 115, 255)     # Steel shadow
C_GOLD = (235, 185, 80, 255)         # EMV chip gold
C_MUTED = (130, 145, 160, 255)       # Secondary typography
C_BORDER = (45, 55, 68, 255)

FONT_BOLD = os.path.join(REPO_DIR, "miniapp/fonts/sila-chest-700.woff2")

def get_font(size):
    return ImageFont.truetype(FONT_BOLD, size)

def make_canvas():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))

def finalize(img, filename):
    final = img.resize((100, 100), Image.Resampling.LANCZOS)
    for pdir in PACK_DIRS:
        out_dir = os.path.join(pdir, "icons")
        path = os.path.join(out_dir, filename)
        final.save(path, "PNG")
    print(f"Generated: {filename} (100x100 PNG RGBA)")

def get_clean_sword(target_w=None, target_h=None, color=C_WHITE):
    """Reuse the customer-derived brand sword monogram."""
    sword_path = os.path.join(REPO_DIR, "miniapp/assets/brand/sword-monogram-light-v2.png")
    source = Image.open(sword_path).convert("RGBA")
    img = Image.new("RGBA", source.size, color)
    img.putalpha(source.getchannel("A"))
    if target_h and not target_w:
        target_w = int(target_h * source.width / source.height)
    elif target_w and not target_h:
        target_h = int(target_w * source.height / source.width)
    elif not target_w and not target_h:
        target_w, target_h = source.width, source.height
    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)

def get_clean_seal(size=400, color=C_WHITE):
    """Extracts authentic round seal from sila-chest-seal-v3.jpg"""
    seal_path = os.path.join(REPO_DIR, "miniapp/assets/sila-chest-seal-v3.jpg")
    im = Image.open(seal_path).convert("L")
    crop = im.crop((17, 13, 490, 485))  # 473 x 472
    arr = np.array(crop).astype(float)
    alpha = np.clip((arr - 25) / (160 - 25) * 255.0, 0, 255).astype(np.uint8)

    H, W = alpha.shape
    Y, X = np.ogrid[:H, :W]
    dist = np.sqrt((X - W/2)**2 + (Y - H/2)**2)
    circle_mask = dist <= (min(W, H)/2 - 2)
    alpha = np.where(circle_mask, alpha, 0)

    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:, :, 0] = color[0]
    rgba[:, :, 1] = color[1]
    rgba[:, :, 2] = color[2]
    rgba[:, :, 3] = alpha

    img = Image.fromarray(rgba, "RGBA")
    return img.resize((size, size), Image.Resampling.LANCZOS)

# =============================================================
# 01. SWORD — Фирменный клинок бренда «СИЛА И ЧЕСТЬ»
# =============================================================
def make_sword():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    sword_h = 550
    sword_raw = get_clean_sword(target_h=sword_h, color=C_WHITE)
    # Morphological dilation so blade and guard remain bold and readable at 20x20
    sword_body = sword_raw.filter(ImageFilter.MaxFilter(7))
    sw_w, sw_h = sword_body.size
    x_off = (S - sw_w) // 2
    y_off = (S - sw_h) // 2

    # High-contrast dark backing silhouette (ensures readability on light Telegram themes)
    sword_shadow = sword_body.filter(ImageFilter.MaxFilter(15))
    shadow_img = Image.new("RGBA", sword_shadow.size, (10, 12, 16, 230))
    shadow_img.putalpha(sword_shadow.getchannel("A"))
    im.alpha_composite(
        shadow_img,
        (x_off - (sword_shadow.width - sw_w) // 2, y_off - (sword_shadow.height - sw_h) // 2)
    )

    # Composite solid white sword
    im.alpha_composite(sword_body, (x_off, y_off))

    # Prominent crimson fuller / blood groove line down center of blade
    cx = S // 2
    fuller_top = y_off + int(sw_h * 0.16)
    fuller_bot = y_off + int(sw_h * 0.88)
    d.line([(cx, fuller_top), (cx, fuller_bot)], fill=C_RED, width=24)
    # Highlighted arrowhead tip of fuller
    d.polygon([(cx, fuller_bot + 24), (cx - 12, fuller_bot), (cx + 12, fuller_bot)], fill=C_RED)

    finalize(im, "01_sword.png")

# =============================================================
# 02. SEAL — Фирменная круглая печать «СИЛА И ЧЕСТЬ»
# =============================================================
def make_seal():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    cx, cy = S // 2, S // 2
    r_outer = 270

    # Outer tactical notched compass ring (heavy, solid)
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=C_CARBON, outline=C_WHITE, width=20)

    # 8 Chunky compass notches (N, NE, E, SE, S, SW, W, NW)
    for deg in range(0, 360, 45):
        rad = math.radians(deg)
        x1 = cx + (r_outer - 8) * math.cos(rad)
        y1 = cy + (r_outer - 8) * math.sin(rad)
        tick_len = 34 if deg % 90 == 0 else 22
        x2 = cx + (r_outer - 8 - tick_len) * math.cos(rad)
        y2 = cy + (r_outer - 8 - tick_len) * math.sin(rad)
        col = C_RED if deg % 90 == 0 else C_WHITE
        w = 14 if deg % 90 == 0 else 10
        d.line([(x1, y1), (x2, y2)], fill=col, width=w)

    # Inner red border ring
    r_inner = 215
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], outline=C_RED, width=12)

    # Center: High-contrast authentic brand sword with monogram
    sw_seal = get_clean_sword(target_h=340, color=C_WHITE).filter(ImageFilter.MaxFilter(5))
    im.alpha_composite(sw_seal, (cx - sw_seal.width // 2, cy - sw_seal.height // 2))

    # Crimson fuller inside the sword
    d.line([(cx, cy - 100), (cx, cy + 130)], fill=C_RED, width=16)
    d.polygon([(cx, cy + 145), (cx - 8, cy + 130), (cx + 8, cy + 130)], fill=C_RED)

    # Tactical inner target accent ring
    d.ellipse([cx - 75, cy - 75, cx + 75, cy + 75], outline=C_RED, width=6)

    finalize(im, "02_seal.png")

# =============================================================
# 03. DROP — Футболка первого дропа «СИЛА И ЧЕСТЬ» (вид спереди)
# =============================================================
def make_drop():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Heavy boxy oversized silhouette
    tee_poly = [
        (300, 115), (240, 102), (145, 158), (55, 290), (140, 345),
        (170, 315), (160, 525), (440, 525), (430, 315),
        (460, 345), (545, 290), (455, 158), (360, 102)
    ]

    # Outer white contour for crisp separation
    d.polygon(tee_poly, fill=C_WHITE)
    # Inner dark heavyweight cotton body
    d.polygon([(x + (300-x)*0.038, y + (320-y)*0.038) for x, y in tee_poly], fill=C_CARBON)

    # Ribbed crewneck collar
    d.ellipse([230, 90, 370, 148], fill=C_WHITE)
    d.ellipse([244, 98, 356, 140], fill=C_CARBON)

    # Wordmark «СИЛА И ЧЕСТЬ» on chest
    wordmark = Image.open(os.path.join(REPO_DIR, "miniapp/assets/brand/sila-chest-wordmark.png")).convert("RGBA")
    wordmark = wordmark.resize((230, 48), Image.Resampling.LANCZOS)
    im.alpha_composite(wordmark, (300 - wordmark.width // 2, 235 - wordmark.height // 2))

    # Red tactical accent dash
    d.rectangle([220, 262, 380, 272], fill=C_RED)

    # Brand sword above chest text
    sw_mini = get_clean_sword(target_h=68, color=C_WHITE).filter(ImageFilter.MaxFilter(3))
    im.alpha_composite(sw_mini, (300 - sw_mini.width // 2, 155))

    # Authentic bottom-left hem patch (jacquard woven label)
    d.rectangle([182, 470, 252, 515], fill=(8, 10, 12, 255), outline=C_WHITE, width=5)
    d.line([190, 492, 244, 492], fill=C_RED, width=5)

    finalize(im, "03_drop.png")

# =============================================================
# 04. ORDERS — Бокс первого дропа / Снабжение («Мои покупки»)
# =============================================================
def make_orders():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Matte black tactical crate with 45° chamfered reinforced corners
    outer_poly = [
        (135, 95), (465, 95), (525, 155), (525, 485),
        (465, 545), (135, 545), (75, 485), (75, 155)
    ]
    d.polygon(outer_poly, fill=C_WHITE)

    inner_poly = [
        (145, 107), (455, 107), (511, 163), (511, 477),
        (455, 533), (145, 533), (89, 477), (89, 163)
    ]
    d.polygon(inner_poly, fill=C_CARBON)

    # Reinforced corner brackets (metal armor tabs) with visible rivets
    for bx, by, flip_x, flip_y in [
        (115, 125, 1, 1), (485, 125, -1, 1),
        (115, 515, 1, -1), (485, 515, -1, -1)
    ]:
        d.polygon([
            (bx, by), (bx + flip_x * 46, by), (bx, by + flip_y * 46)
        ], fill=C_STEEL_MID)
        # Screw rivet
        d.ellipse([bx + flip_x * 12 - 5, by + flip_y * 12 - 5, bx + flip_x * 12 + 5, by + flip_y * 12 + 5], fill=C_WHITE)

    # Center white brand sword emblem
    sw_crate = get_clean_sword(target_h=160, color=C_WHITE).filter(ImageFilter.MaxFilter(3))
    im.alpha_composite(sw_crate, (300 - sw_crate.width // 2, 140))
    d.line([(300, 165), (300, 275)], fill=C_RED, width=7)

    # Stencil text DROP 001
    f_crate = get_font(32)
    d.text((300, 345), "DROP 001", font=f_crate, fill=C_WHITE, anchor="mm")
    f_sub = get_font(22)
    d.text((300, 385), "СИЛА И ЧЕСТЬ", font=f_sub, fill=C_MUTED, anchor="mm")

    # Shipping barcode & red inspection stamp
    for x in range(150, 450, 16):
        w = 8 if (x // 16) % 3 == 0 else 4
        d.line([(x, 430), (x, 485)], fill=C_WHITE, width=w)

    # Red inspection stamp
    d.rectangle([375, 420, 480, 485], outline=C_RED, width=6)
    f_stamp = get_font(22)
    d.text((427, 452), "OK", font=f_stamp, fill=C_RED, anchor="mm")

    finalize(im, "04_orders.png")

# =============================================================
# 05. PROFILE — Стальной жетон «СИЛА И ЧЕСТЬ» (Личный кабинет)
# =============================================================
def make_profile():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Top ball chain loops (sturdy beads)
    for cx, cy in [(270, 70), (250, 48), (235, 26), (330, 70), (350, 48), (365, 26)]:
        d.ellipse([cx-15, cy-15, cx+15, cy+15], fill=C_STEEL_MID, outline=C_WHITE, width=4)
        d.ellipse([cx-6, cy-6, cx+3, cy+3], fill=C_WHITE)  # Specular shine

    # Dog tag outer rim
    d.rounded_rectangle([155, 75, 445, 530], radius=74, fill=C_STEEL_LO, outline=C_WHITE, width=12)
    # Beveled step
    d.rounded_rectangle([168, 87, 432, 518], radius=64, fill=C_STEEL_MID)
    # Brushed steel face plate
    d.rounded_rectangle([180, 99, 420, 506], radius=54, fill=C_STEEL_HI)

    # Top stamped chain hole
    d.ellipse([272, 115, 328, 171], fill=(20, 25, 32, 255), outline=C_STEEL_LO, width=7)

    # Authentic laser-engraved circular brand sword in center
    sw_tag = get_clean_sword(target_h=190, color=(24, 30, 38, 255)).filter(ImageFilter.MaxFilter(3))
    im.alpha_composite(sw_tag, (300 - sw_tag.width // 2, 185))

    # Red accent circle inside seal
    d.ellipse([265, 250, 335, 320], outline=C_RED, width=5)

    # Laser-etched typography
    f_tag = get_font(28)
    d.text((300, 395), "СИЛА И ЧЕСТЬ", font=f_tag, fill=(24, 30, 38, 255), anchor="mm")
    f_tag_sub = get_font(22)
    d.text((300, 435), "ID: 001 · STEEL", font=f_tag_sub, fill=(90, 105, 120, 255), anchor="mm")
    # Red status indicator
    d.ellipse([292, 468, 308, 484], fill=C_RED)

    finalize(im, "05_profile.png")

# =============================================================
# 06. SUPPORT — Штабная связь / Поддержка («СИЛА И ЧЕСТЬ»)
# =============================================================
def make_support():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # 1. Thick armored headband arc
    d.arc([110, 60, 490, 420], start=180, end=0, fill=C_WHITE, width=44)
    d.arc([132, 82, 468, 398], start=180, end=0, fill=C_CARBON, width=22)

    # Tactical red comfort cushion blocks on top
    for x in [225, 275, 325]:
        d.rounded_rectangle([x, 48, x + 38, 76], radius=6, fill=C_RED, outline=C_WHITE, width=4)

    # 2. Armored earcups (bold heavy-duty blocks)
    for ex in [75, 445]:
        d.rounded_rectangle([ex, 210, ex + 80, 390], radius=24, fill=C_WHITE)
        d.rounded_rectangle([ex + 8, 218, ex + 72, 382], radius=18, fill=C_CARBON)
        # Brushed steel center plate
        d.rounded_rectangle([ex + 18, 240, ex + 62, 360], radius=12, fill=C_STEEL_MID)
        # Red tactical status indicator
        d.rectangle([ex + 28, 285, ex + 52, 315], fill=C_RED)

    # 3. Heavy microphone boom arm
    d.line([(120, 360), (180, 470), (280, 470)], fill=C_WHITE, width=22)
    d.line([(120, 360), (180, 470), (280, 470)], fill=C_CARBON, width=10)

    # Red microphone capsule at the end of the boom
    d.rounded_rectangle([280, 448, 345, 492], radius=12, fill=C_RED, outline=C_WHITE, width=6)
    d.rectangle([335, 456, 345, 484], fill=C_WHITE)  # Microphone mesh tip

    # 4. Center brand sword monogram
    sw_mini = get_clean_sword(target_h=200, color=C_WHITE).filter(ImageFilter.MaxFilter(5))
    im.alpha_composite(sw_mini, (300 - sw_mini.width // 2, 210))
    d.line([(300, 245), (300, 375)], fill=C_RED, width=10)

    # 5. Transmission waves emanating from mic
    for r, col, w in [(60, C_WHITE, 8), (95, C_RED, 10), (130, C_WHITE, 10)]:
        d.arc([310 - r, 470 - r, 310 + r, 470 + r], start=-40, end=40, fill=col, width=w)

    finalize(im, "06_support.png")

# =============================================================
# 07. CART — ВИТРИНА / МАГАЗИН («Открыть витрину» / «Оформить заказ»)
# =============================================================
def make_cart():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Authentic Brand Storefront / Showcase Vitrina
    # 1. Top Canopy / Awning (козырек витрины)
    awning_poly = [
        (75, 145), (115, 85), (485, 85), (525, 145),
        (505, 165), (95, 165)
    ]
    d.polygon(awning_poly, fill=C_WHITE)
    # Inner dark canopy body
    d.polygon([
        (87, 141), (123, 93), (477, 93), (513, 141),
        (497, 157), (103, 157)
    ], fill=C_CARBON)
    # Crimson neon front edge along canopy visor
    d.line([(95, 160), (505, 160)], fill=C_RED, width=14)

    # 2. Main Storefront Vitrina Box (панорамная витрина)
    d.rounded_rectangle([85, 160, 515, 485], radius=16, fill=C_WHITE)
    d.rounded_rectangle([97, 172, 503, 473], radius=10, fill=C_CARBON)

    # Storefront glass reflection glare lines
    d.polygon([(105, 180), (195, 180), (135, 465), (105, 465)], fill=(45, 60, 80, 80))
    d.polygon([(225, 180), (255, 180), (165, 465), (145, 465)], fill=(45, 60, 80, 60))

    # 3. Inside Vitrina: Illuminated Brand Sword on Pedestal
    # Illuminated spotlight glow behind sword
    d.ellipse([200, 185, 400, 445], fill=(225, 6, 0, 40))

    # Brand Sword centered in showcase window
    sw_display = get_clean_sword(target_h=255, color=C_WHITE).filter(ImageFilter.MaxFilter(5))
    im.alpha_composite(sw_display, (300 - sw_display.width // 2, 200))
    # Red fuller in displayed sword
    d.line([(300, 240), (300, 410)], fill=C_RED, width=10)

    # Pedestal under the sword
    d.rounded_rectangle([215, 430, 385, 460], radius=6, fill=C_STEEL_HI, outline=C_WHITE, width=4)
    d.line([(235, 445), (365, 445)], fill=C_RED, width=4)

    # 4. Storefront Base / Plinth & Step (цоколь магазина)
    d.rounded_rectangle([65, 480, 535, 530], radius=8, fill=C_WHITE)
    d.rounded_rectangle([73, 488, 527, 522], radius=4, fill=C_DARK)
    # Entrance threshold / red brand runner
    d.rectangle([200, 488, 400, 522], fill=C_RED)

    # Side pillar accent tabs
    for px in [91, 509]:
        d.rectangle([px - 5, 170, px + 5, 475], fill=C_STEEL_MID)
        d.rectangle([px - 3, 215, px + 3, 255], fill=C_RED)

    finalize(im, "07_cart.png")

# =============================================================
# 08. RULER — Тканый шеврон у подола / Замеры (tee-detail-v6.jpg)
# =============================================================
def make_ruler():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Authentic jacquard woven hem label from the real t-shirt
    d.rounded_rectangle([85, 125, 515, 475], radius=20, fill=C_WHITE)
    d.rounded_rectangle([95, 135, 505, 465], radius=16, fill=C_CARBON)

    # Running stitch lines along perimeter (white dashed sewing threads)
    for side in ["top", "bottom"]:
        y = 152 if side == "top" else 448
        for x in range(115, 485, 22):
            d.line([(x, y), (x + 14, y)], fill=C_WHITE, width=6)
    for side in ["left", "right"]:
        x = 112 if side == "left" else 488
        for y in range(155, 445, 22):
            d.line([(x, y), (x, y + 14)], fill=C_WHITE, width=6)

    # Center embroidered authentic sword logo
    sw_patch = get_clean_sword(target_h=225, color=C_WHITE).filter(ImageFilter.MaxFilter(5))
    im.alpha_composite(sw_patch, (300 - sw_patch.width // 2, 165))

    # Red embroidered fuller
    d.line([(300, 200), (300, 345)], fill=C_RED, width=8)

    # Bottom embroidered brand text: «СИЛА И ЧЕСТЬ»
    f_patch = get_font(28)
    d.text((300, 405), "СИЛА И ЧЕСТЬ", font=f_patch, fill=C_WHITE, anchor="mm")

    # Red woven flag tag extending from left seam
    d.rectangle([55, 255, 95, 345], fill=C_RED)
    d.ellipse([70, 285, 80, 315], fill=C_WHITE)

    finalize(im, "08_ruler.png")

# =============================================================
# 09. CARD — Титановый токен / Оплата дропа
# =============================================================
def make_card():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Matte titanium metal payment card with chamfered corner
    card_poly = [
        (85, 135), (460, 135), (515, 190), (515, 465),
        (85, 465)
    ]
    d.polygon(card_poly, fill=C_WHITE)
    d.polygon([(x + (300-x)*0.032, y + (300-y)*0.032) for x, y in card_poly], fill=C_CARBON)

    # EMV Gold metallic chip
    d.rounded_rectangle([135, 205, 230, 285], radius=14, fill=C_GOLD, outline=C_WHITE, width=4)
    d.line([(135, 245), (230, 245)], fill=(180, 135, 40, 255), width=4)
    d.line([(182, 205), (182, 285)], fill=(180, 135, 40, 255), width=4)

    # Contactless wave arcs
    for r in [22, 40, 58]:
        d.arc([245 - r, 245 - r, 245 + r, 245 + r], start=-45, end=45, fill=C_WHITE, width=6)

    # Laser watermark of brand sword on card surface
    sw_watermark = get_clean_sword(target_h=270, color=(55, 68, 85, 255))
    im.alpha_composite(sw_watermark, (375 - sw_watermark.width // 2, 165))

    # Embossed typography
    f_num = get_font(32)
    d.text((135, 350), "•••• 001", font=f_num, fill=C_WHITE, anchor="lm")
    f_holder = get_font(24)
    d.text((135, 410), "СИЛА И ЧЕСТЬ", font=f_holder, fill=C_MUTED, anchor="lm")

    # Red security hologram badge
    d.rectangle([420, 390, 480, 435], fill=C_RED)

    finalize(im, "09_card.png")

# =============================================================
# 10. LOOKBOOK — Спина дропа с длинным клинком (tee-back-final.jpg)
# =============================================================
def make_lookbook():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    # Silhouette of oversized t-shirt seen from behind
    tee_poly = [
        (300, 110), (240, 102), (145, 158), (55, 290), (140, 345),
        (170, 315), (160, 525), (440, 525), (430, 315),
        (460, 345), (545, 290), (455, 158), (360, 102)
    ]
    d.polygon(tee_poly, fill=C_WHITE)
    d.polygon([(x + (300-x)*0.035, y + (320-y)*0.035) for x, y in tee_poly], fill=C_CARBON)

    # Back collar arc
    d.arc([235, 96, 365, 130], start=0, end=180, fill=C_WHITE, width=10)

    # Circular neck brand mark at the nape
    d.ellipse([282, 138, 318, 174], fill=C_RED, outline=C_WHITE, width=4)

    # Full spine distressed sword spanning from collar to hem
    sw_back = get_clean_sword(target_h=340, color=C_WHITE).filter(ImageFilter.MaxFilter(5))
    im.alpha_composite(sw_back, (300 - sw_back.width // 2, 175))

    # Crimson spine fuller line
    d.line([(300, 210), (300, 495)], fill=C_RED, width=10)

    finalize(im, "10_lookbook.png")

# =============================================================
# 11. CHANNEL — Канал бренда (Тактическая радиовышка / Сигнал)
# =============================================================
def make_channel():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    cx, cy = 300, 110

    # 1. Concentric broadcast signal waves radiating left and right
    for r, col, w in [
        (130, C_RED, 22),
        (190, C_WHITE, 26),
        (250, C_RED, 22),
        (310, C_WHITE, 24)
    ]:
        # Left wave arcs
        d.arc([cx - r, cy - r, cx + r, cy + r], start=125, end=215, fill=col, width=w)
        # Right wave arcs
        d.arc([cx - r, cy - r, cx + r, cy + r], start=-35, end=55, fill=col, width=w)

    # 2. Main mast and transmitter antenna beacon at top
    d.line([(cx, 80), (cx, 260)], fill=C_WHITE, width=26)
    # Bright red transmitter dome beacon
    d.ellipse([cx - 42, 60, cx + 42, 144], fill=C_RED, outline=C_WHITE, width=10)
    d.ellipse([cx - 16, 86, cx + 16, 118], fill=C_WHITE)  # White glowing core

    # 3. Heavy A-frame tower lattice legs
    # Left leg
    d.polygon([(cx - 15, 200), (cx + 5, 200), (180, 540), (120, 540)], fill=C_WHITE)
    d.polygon([(cx - 10, 210), (cx + 2, 210), (170, 532), (130, 532)], fill=C_CARBON)
    # Right leg
    d.polygon([(cx - 5, 200), (cx + 15, 200), (480, 540), (420, 540)], fill=C_WHITE)
    d.polygon([(cx - 2, 210), (cx + 10, 210), (470, 532), (430, 532)], fill=C_CARBON)

    # 4. Heavy cross braces (lattice trusses)
    for y, w_bar in [(270, 16), (350, 20), (430, 24), (510, 26)]:
        span = int((y - 180) * 0.46)
        d.line([(cx - span, y), (cx + span, y)], fill=C_WHITE, width=w_bar)

    # Diagonal X-bracing in lower sections
    d.line([(cx - 65, 350), (cx + 115, 430)], fill=C_STEEL_MID, width=12)
    d.line([(cx + 65, 350), (cx - 115, 430)], fill=C_STEEL_MID, width=12)

    # 5. Mounted brand sword shield on center of tower
    d.rounded_rectangle([248, 280, 352, 420], radius=16, fill=C_CARBON, outline=C_WHITE, width=8)
    d.rounded_rectangle([256, 288, 344, 412], radius=10, outline=C_RED, width=5)
    sw_tower = get_clean_sword(target_h=110, color=C_WHITE).filter(ImageFilter.MaxFilter(3))
    im.alpha_composite(sw_tower, (300 - sw_tower.width // 2, 295))
    d.line([(300, 315), (300, 390)], fill=C_RED, width=6)

    finalize(im, "11_channel.png")

# =============================================================
# 12. BACK — Шеврон возврата / Главное меню
# =============================================================
def make_back():
    im = make_canvas()
    d = ImageDraw.Draw(im)

    def draw_chevron(offset_x, fill_col, border_col, red_center=False):
        points = [
            (290 + offset_x, 90), (360 + offset_x, 90),
            (180 + offset_x, 300), (360 + offset_x, 510),
            (290 + offset_x, 510), (110 + offset_x, 300)
        ]
        d.polygon(points, fill=fill_col, outline=border_col)
        if red_center:
            d.polygon([
                (290 + offset_x, 140), (325 + offset_x, 140),
                (195 + offset_x, 300), (325 + offset_x, 460),
                (290 + offset_x, 460), (160 + offset_x, 300)
            ], fill=C_RED)

    # Right chevron (dark plate with white edge)
    draw_chevron(110, C_DARK, C_WHITE, red_center=False)
    # Left chevron (lead chevron with crimson core)
    draw_chevron(-40, C_CARBON, C_WHITE, red_center=True)

    finalize(im, "12_back.png")

# =============================================================
# Preview Grid Generator
# =============================================================
def make_preview_grid():
    cols, rows = 4, 3
    cell_w, cell_h = 180, 180
    pad = 40
    title_h = 100

    gw = cols * cell_w + pad * 2
    gh = rows * cell_h + pad * 2 + title_h

    bg = Image.new("RGBA", (gw, gh), (11, 14, 18, 255))
    d = ImageDraw.Draw(bg)

    # Header title
    f_title = get_font(28)
    d.text((gw // 2, 45), "СИЛА И ЧЕСТЬ · BRAND CUSTOM EMOJI SET", font=f_title, fill=C_WHITE, anchor="mm")
    f_sub = get_font(18)
    d.text((gw // 2, 80), "АУТЕНТИЧНЫЙ ТАКТИЧЕСКИЙ ПАК 100×100 PNG ПО ДИЗАЙНУ МЕРЧА", font=f_sub, fill=C_RED, anchor="mm")

    labels = [
        ("01_sword.png", "01. КЛИНОК"),
        ("02_seal.png", "02. ПЕЧАТЬ"),
        ("03_drop.png", "03. ДРОП (ПЕРЕД)"),
        ("04_orders.png", "04. БОКС ДРОПА"),
        ("05_profile.png", "05. ЖЕТОН СТАЛЬ"),
        ("06_support.png", "06. ШТАБ-СВЯЗЬ"),
        ("07_cart.png", "07. ВИТРИНА ДРОПА"),
        ("08_ruler.png", "08. ШЕВРОН ПОДОЛА"),
        ("09_card.png", "09. ТИТАН-КАРТА"),
        ("10_lookbook.png", "10. СПИНА С КЛИНКОМ"),
        ("11_channel.png", "11. СИГНАЛ КАНАЛА"),
        ("12_back.png", "12. ШЕВРОН НАЗАД")
    ]

    f_lbl = get_font(16)

    for idx, (fname, title) in enumerate(labels):
        c = idx % cols
        r = idx // cols
        x = pad + c * cell_w
        y = pad + title_h + r * cell_h

        d.rounded_rectangle([x + 8, y + 8, x + cell_w - 8, y + cell_h - 8], radius=16, fill=(20, 25, 32, 255), outline=C_BORDER, width=2)

        icon_path = os.path.join(PACK_DIRS[0], "icons", fname)
        if os.path.exists(icon_path):
            icon = Image.open(icon_path).resize((88, 88), Image.Resampling.LANCZOS)
            bg.alpha_composite(icon, (x + (cell_w - 88) // 2, y + 22))

        d.text((x + cell_w // 2, y + cell_h - 26), title, font=f_lbl, fill=C_MUTED, anchor="mm")

    for pdir in PACK_DIRS:
        grid_path = os.path.join(pdir, "preview_grid.png")
        bg.save(grid_path, "PNG")
        print(f"Generated preview grid: {grid_path}")

def make_zip_pack():
    zip_path = os.path.join(PACK_DIRS[0], "sila_chest_emoji_pack.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fname in sorted(os.listdir(os.path.join(PACK_DIRS[0], "icons"))):
            if fname.endswith(".png"):
                full_path = os.path.join(PACK_DIRS[0], "icons", fname)
                z.write(full_path, arcname=fname)
    print(f"Created ZIP pack: {zip_path}")

if __name__ == "__main__":
    print("Generating authentic brand emoji set...")
    make_sword()
    make_seal()
    make_drop()
    make_orders()
    make_profile()
    make_support()
    make_cart()
    make_ruler()
    make_card()
    make_lookbook()
    make_channel()
    make_back()
    make_preview_grid()
    make_zip_pack()
    print("All icons, preview grid and zip package successfully generated!")
