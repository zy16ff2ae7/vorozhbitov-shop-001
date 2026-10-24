import os
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import numpy as np

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_DIR, "custom_emoji_pack/icons")
os.makedirs(OUT_DIR, exist_ok=True)

# High-resolution supersampling canvas (600x600 -> 100x100 via Lanczos)
S = 600

# Color palette: Authentic "СИЛА И ЧЕСТЬ" tactical streetwear
C_WHITE = (255, 255, 255, 255)
C_RED = (225, 6, 0, 255)          # Tactical crimson accent
C_CARBON = (13, 16, 20, 255)       # Matte streetwear cotton / carbon black
C_DARK = (20, 25, 32, 255)         # Armor plate dark
C_STEEL_HI = (225, 235, 245, 255)  # Cold brushed steel highlight
C_STEEL_MID = (165, 180, 195, 255) # Steel midtone
C_STEEL_LO = (85, 100, 115, 255)   # Steel shadow
C_GOLD = (235, 185, 80, 255)       # EMV chip gold
C_MUTED = (130, 145, 160, 255)     # Secondary typography
C_BORDER = (45, 55, 68, 255)

FONT_BOLD = os.path.join(REPO_DIR, "miniapp/fonts/sila-chest-700.woff2")

def get_font(size):
    return ImageFont.truetype(FONT_BOLD, size)

def make_canvas():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))

def finalize(img, filename):
    final = img.resize((100, 100), Image.Resampling.LANCZOS)
    path = os.path.join(OUT_DIR, filename)
    final.save(path, "PNG")
    print(f"Generated: {filename} (100x100 PNG RGBA)")

def get_clean_sword(target_w=None, target_h=None, color=C_WHITE):
    """Extracts authentic sword logo from welcome-sword-logo.jpg"""
    sword_path = os.path.join(REPO_DIR, "miniapp/assets/welcome-sword-logo.jpg")
    im = Image.open(sword_path).convert("L")
    crop = im.crop((245, 16, 961, 1295)) # 716 x 1279
    arr = np.array(crop).astype(float)
    alpha = np.clip((arr - 15) / (120 - 15) * 255.0, 0, 255).astype(np.uint8)
    
    rgba = np.zeros((crop.height, crop.width, 4), dtype=np.uint8)
    rgba[:,:,0] = color[0]
    rgba[:,:,1] = color[1]
    rgba[:,:,2] = color[2]
    rgba[:,:,3] = alpha
    
    img = Image.fromarray(rgba, "RGBA")
    if target_h and not target_w:
        target_w = int(target_h * crop.width / crop.height)
    elif target_w and not target_h:
        target_h = int(target_w * crop.height / crop.width)
    elif not target_w and not target_h:
        target_w, target_h = crop.width, crop.height
        
    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)

def get_clean_seal(size=400, color=C_WHITE):
    """Extracts authentic round seal from sila-chest-seal-v2.jpg"""
    seal_path = os.path.join(REPO_DIR, "miniapp/assets/sila-chest-seal-v2.jpg")
    im = Image.open(seal_path).convert("L")
    crop = im.crop((17, 13, 490, 485)) # 473 x 472
    arr = np.array(crop).astype(float)
    alpha = np.clip((arr - 25) / (160 - 25) * 255.0, 0, 255).astype(np.uint8)
    
    H, W = alpha.shape
    Y, X = np.ogrid[:H, :W]
    dist = np.sqrt((X - W/2)**2 + (Y - H/2)**2)
    circle_mask = dist <= (min(W, H)/2 - 2)
    alpha = np.where(circle_mask, alpha, 0)
    
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:,:,0] = color[0]
    rgba[:,:,1] = color[1]
    rgba[:,:,2] = color[2]
    rgba[:,:,3] = alpha
    
    img = Image.fromarray(rgba, "RGBA")
    return img.resize((size, size), Image.Resampling.LANCZOS)

# =============================================================
# 01. SWORD — Фирменный клинок бренда «СИЛА И ЧЕСТЬ»
# =============================================================
def make_sword():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Extract authentic sword sized to height 540
    sword_h = 540
    sword_raw = get_clean_sword(target_h=sword_h, color=C_WHITE)
    sw_w, sw_h = sword_raw.size
    x_off = (S - sw_w) // 2
    y_off = (S - sw_h) // 2
    
    # Outer dark glow/backing for contrast on light theme
    sword_shadow = get_clean_sword(target_h=sword_h + 16, color=(10, 12, 16, 220))
    im.alpha_composite(sword_shadow, ((S - sword_shadow.width)//2, (S - sword_shadow.height)//2))
    
    # Authentic sword body
    im.alpha_composite(sword_raw, (x_off, y_off))
    
    # Crimson fuller / blood groove line down center of blade
    # Blade is vertically centered, fuller runs from roughly y=120 to y=490
    cx = S // 2
    d.line([(cx, y_off + int(sw_h * 0.16)), (cx, y_off + int(sw_h * 0.88))], fill=C_RED, width=14)
    # Highlight tip of fuller
    d.polygon([(cx, y_off + int(sw_h * 0.92)), (cx - 7, y_off + int(sw_h * 0.88)), (cx + 7, y_off + int(sw_h * 0.88))], fill=C_RED)
    
    finalize(im, "01_sword.png")

# =============================================================
# 02. SEAL — Фирменная круглая печать «СИЛА И ЧЕСТЬ»
# =============================================================
def make_seal():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    cx, cy = S // 2, S // 2
    r_outer = 270
    
    # Outer tactical notched compass ring
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=C_CARBON, outline=C_WHITE, width=12)
    # Radial tick marks around perimeter
    for deg in range(0, 360, 15):
        rad = math.radians(deg)
        x1 = cx + (r_outer - 6) * math.cos(rad)
        y1 = cy + (r_outer - 6) * math.sin(rad)
        tick_len = 22 if deg % 45 == 0 else 12
        x2 = cx + (r_outer - 6 - tick_len) * math.cos(rad)
        y2 = cy + (r_outer - 6 - tick_len) * math.sin(rad)
        col = C_RED if deg % 90 == 0 else C_WHITE
        w = 6 if deg % 45 == 0 else 3
        d.line([(x1, y1), (x2, y2)], fill=col, width=w)
        
    # Inner red border ring
    r_inner = 230
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], outline=C_RED, width=6)
    
    # Composite authentic brand seal insignia
    seal_art = get_clean_seal(size=420, color=C_WHITE)
    im.alpha_composite(seal_art, (cx - 210, cy - 210))
    
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
    d.polygon([(x + (300-x)*0.035, y + (320-y)*0.035) for x, y in tee_poly], fill=C_CARBON)
    
    # Ribbed crewneck collar
    d.ellipse([235, 92, 365, 145], fill=C_WHITE)
    d.ellipse([247, 100, 353, 137], fill=C_CARBON)
    
    # Exact reference contours for the small chest print; other captions use Sila Chest.
    wordmark = Image.open(os.path.join(REPO_DIR, "miniapp/assets/brand/sila-chest-wordmark.png")).convert("RGBA")
    wordmark = wordmark.resize((220, 46), Image.Resampling.LANCZOS)
    im.alpha_composite(wordmark, (300 - wordmark.width // 2, 235 - wordmark.height // 2))
    # Red tactical accent dash
    d.rectangle([230, 260, 370, 268], fill=C_RED)
    
    # Small sword icon on chest above text
    sw_mini = get_clean_sword(target_h=60, color=C_WHITE)
    im.alpha_composite(sw_mini, (300 - sw_mini.width // 2, 160))
    
    # Authentic bottom-left hem patch (jacquard label)
    d.rectangle([185, 475, 245, 515], fill=(8, 10, 12, 255), outline=C_WHITE, width=4)
    d.line([192, 495, 238, 495], fill=C_RED, width=4)
    
    finalize(im, "03_drop.png")

# =============================================================
# 04. ORDERS — Бокс первого дропа / Снабжение («Мои покупки»)
# =============================================================
def make_orders():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Matte black tactical crate with 45° chamfered reinforced corners
    # Outer white armor outline
    outer_poly = [
        (140, 100), (460, 100), (520, 160), (520, 480),
        (460, 540), (140, 540), (80, 480), (80, 160)
    ]
    d.polygon(outer_poly, fill=C_WHITE)
    
    # Inner carbon body
    inner_poly = [
        (148, 110), (452, 110), (508, 166), (508, 472),
        (452, 528), (148, 528), (92, 472), (92, 166)
    ]
    d.polygon(inner_poly, fill=C_CARBON)
    
    # Reinforced corner brackets (metal armor tabs)
    for bx, by, flip_x, flip_y in [
        (120, 130, 1, 1), (480, 130, -1, 1),
        (120, 510, 1, -1), (480, 510, -1, -1)
    ]:
        d.polygon([
            (bx, by), (bx + flip_x * 40, by), (bx, by + flip_y * 40)
        ], fill=C_STEEL_MID)
        # Screw rivet
        d.ellipse([bx + flip_x * 8 - 4, by + flip_y * 8 - 4, bx + flip_x * 8 + 4, by + flip_y * 8 + 4], fill=C_WHITE)
        
    # Center stamped white brand seal
    seal_mini = get_clean_seal(size=180, color=C_WHITE)
    im.alpha_composite(seal_mini, (300 - 90, 150))
    
    # Stencil text
    f_crate = get_font(28)
    d.text((300, 365), "DROP 001", font=f_crate, fill=C_WHITE, anchor="mm")
    f_sub = get_font(20)
    d.text((300, 405), "СИЛА И ЧЕСТЬ", font=f_sub, fill=C_MUTED, anchor="mm")
    
    # Shipping barcode & red inspection stamp
    # Barcode
    for x in range(160, 440, 14):
        w = 6 if (x // 14) % 3 == 0 else 3
        d.line([(x, 440), (x, 485)], fill=C_WHITE, width=w)
    # Red inspection stamp
    d.rectangle([380, 435, 475, 490], outline=C_RED, width=4)
    f_stamp = get_font(18)
    d.text((427, 462), "OK", font=f_stamp, fill=C_RED, anchor="mm")
    
    finalize(im, "04_orders.png")

# =============================================================
# 05. PROFILE — Стальной жетон «СИЛА И ЧЕСТЬ» (по tag-sich-01-v2.jpg)
# =============================================================
def make_profile():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Top ball chain loops
    for cx, cy in [(270, 70), (250, 48), (235, 26), (330, 70), (350, 48), (365, 26)]:
        d.ellipse([cx-14, cy-14, cx+14, cy+14], fill=C_STEEL_MID, outline=C_WHITE, width=3)
        d.ellipse([cx-6, cy-6, cx+2, cy+2], fill=C_WHITE) # Specular shine
        
    # Dog tag outer beveled rim (width 280, height 450, radius 70)
    d.rounded_rectangle([160, 75, 440, 525], radius=72, fill=C_STEEL_LO, outline=C_WHITE, width=8)
    # Beveled step
    d.rounded_rectangle([170, 85, 430, 515], radius=64, fill=C_STEEL_MID)
    # Brushed steel face plate
    d.rounded_rectangle([182, 97, 418, 503], radius=54, fill=C_STEEL_HI)
    
    # Top stamped chain hole
    d.ellipse([275, 115, 325, 165], fill=(20, 25, 32, 255), outline=C_STEEL_LO, width=6)
    
    # Authentic laser-engraved circular brand seal in center
    seal_engraved = get_clean_seal(size=180, color=(24, 30, 38, 255))
    im.alpha_composite(seal_engraved, (300 - 90, 195))
    
    # Red accent circle inside seal
    d.ellipse([270, 255, 330, 315], outline=C_RED, width=4)
    
    # Laser-etched typography
    f_tag = get_font(26)
    d.text((300, 400), "СИЛА И ЧЕСТЬ", font=f_tag, fill=(24, 30, 38, 255), anchor="mm")
    f_tag_sub = get_font(20)
    d.text((300, 440), "ID: 001 · STEEL", font=f_tag_sub, fill=(90, 105, 120, 255), anchor="mm")
    # Red status indicator
    d.ellipse([294, 472, 306, 484], fill=C_RED)
    
    finalize(im, "05_profile.png")

# =============================================================
# 06. SUPPORT — Штабная связь / Поддержка («СИЛА И ЧЕСТЬ»)
# =============================================================
def make_support():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Tactical military communications headset with brand shield & waves
    # Headband arc
    d.arc([130, 70, 470, 410], start=180, end=0, fill=C_WHITE, width=28)
    d.arc([145, 85, 455, 395], start=180, end=0, fill=C_CARBON, width=12)
    # Tactical padding blocks on top of headband
    for x in [240, 280, 320]:
        d.rectangle([x, 62, x + 24, 82], fill=C_RED)
        
    # Earcups (armored tactical blocks)
    for ex in [100, 450]:
        d.rounded_rectangle([ex, 230, ex + 50, 370], radius=16, fill=C_WHITE)
        d.rounded_rectangle([ex + 6, 236, ex + 44, 364], radius=12, fill=C_CARBON)
        d.rectangle([ex + 14, 260, ex + 36, 340], fill=C_STEEL_MID)
        
    # Microphone boom arm
    d.line([(125, 350), (170, 450), (250, 450)], fill=C_WHITE, width=14)
    d.rounded_rectangle([250, 435, 295, 465], radius=8, fill=C_RED, outline=C_WHITE, width=4)
    
    # Center tactical insignia (brand sword)
    sw_mini = get_clean_sword(target_h=180, color=C_WHITE)
    im.alpha_composite(sw_mini, (300 - sw_mini.width // 2, 210))
    
    # Signal waves emanating from center
    for r, col, w in [(120, C_WHITE, 6), (150, C_RED, 8)]:
        d.arc([300 - r, 300 - r, 300 + r, 300 + r], start=210, end=330, fill=col, width=w)
        
    finalize(im, "06_support.png")

# =============================================================
# 07. CART — Оформление заказа / Тактический кофр дропа
# =============================================================
def make_cart():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Tactical heavy equipment case / transport trunk with MOLLE webbing
    # Handle on top
    d.rounded_rectangle([220, 70, 380, 160], radius=24, outline=C_WHITE, width=20)
    d.rectangle([260, 60, 340, 110], fill=C_RED) # Grip wrap
    
    # Main case body
    d.rounded_rectangle([70, 150, 530, 510], radius=32, fill=C_WHITE)
    d.rounded_rectangle([82, 162, 518, 498], radius=24, fill=C_CARBON)
    
    # Horizontal case split line (lid / base)
    d.line([(82, 270), (518, 270)], fill=C_WHITE, width=8)
    
    # Steel latches (left & right)
    for lx in [170, 380]:
        d.rounded_rectangle([lx, 240, lx + 50, 300], radius=8, fill=C_STEEL_HI, outline=C_WHITE, width=3)
        d.line([lx + 10, 270, lx + 40, 270], fill=C_RED, width=4)
        
    # MOLLE webbing rows on bottom half
    for wy in [340, 395, 450]:
        d.line([(120, wy), (480, wy)], fill=C_STEEL_LO, width=6)
        for wx in range(160, 480, 60):
            d.line([(wx, wy - 10), (wx, wy + 10)], fill=C_WHITE, width=4)
            
    # Center brand seal on top lid
    seal_mini = get_clean_seal(size=90, color=C_WHITE)
    im.alpha_composite(seal_mini, (300 - 45, 172))
    
    finalize(im, "07_cart.png")

# =============================================================
# 08. RULER — Тканый шеврон у подола / Замеры (tee-detail-v5.jpg)
# =============================================================
def make_ruler():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Authentic jacquard woven hem label from the real t-shirt
    # Slanted rectangular tactical fabric patch (width 400, height 320)
    d.rounded_rectangle([90, 130, 510, 470], radius=18, fill=C_WHITE)
    d.rounded_rectangle([98, 138, 502, 462], radius=14, fill=C_CARBON)
    
    # Running stitch lines along perimeter (white dashed sewing threads)
    for side in ["top", "bottom"]:
        y = 154 if side == "top" else 446
        for x in range(120, 485, 20):
            d.line([(x, y), (x + 12, y)], fill=C_WHITE, width=4)
    for side in ["left", "right"]:
        x = 114 if side == "left" else 486
        for y in range(160, 445, 20):
            d.line([(x, y), (x, y + 12)], fill=C_WHITE, width=4)
            
    # Center embroidered authentic sword logo
    sw_patch = get_clean_sword(target_h=210, color=C_WHITE)
    im.alpha_composite(sw_patch, (300 - sw_patch.width // 2, 170))
    
    # Red embroidered fuller
    d.line([(300, 205), (300, 345)], fill=C_RED, width=6)
    
    # Bottom embroidered brand text: «СИЛА И ЧЕСТЬ»
    f_patch = get_font(26)
    d.text((300, 405), "СИЛА И ЧЕСТЬ", font=f_patch, fill=C_WHITE, anchor="mm")
    
    # Red woven flag tag extending from left seam
    d.rectangle([60, 260, 98, 340], fill=C_RED)
    d.ellipse([75, 290, 85, 310], fill=C_WHITE)
    
    finalize(im, "08_ruler.png")

# =============================================================
# 09. CARD — Титановый токен / Оплата дропа
# =============================================================
def make_card():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Matte titanium metal payment card with chamfered corner
    card_poly = [
        (90, 140), (460, 140), (510, 190), (510, 460),
        (90, 460)
    ]
    d.polygon(card_poly, fill=C_WHITE)
    d.polygon([(x + (300-x)*0.03, y + (300-y)*0.03) for x, y in card_poly], fill=C_CARBON)
    
    # EMV Gold metallic chip
    d.rounded_rectangle([140, 210, 225, 285], radius=12, fill=C_GOLD, outline=C_WHITE, width=3)
    d.line([(140, 247), (225, 247)], fill=(180, 135, 40, 255), width=3)
    d.line([(182, 210), (182, 285)], fill=(180, 135, 40, 255), width=3)
    
    # Contactless wave arcs
    for r in [20, 36, 52]:
        d.arc([240 - r, 248 - r, 240 + r, 248 + r], start=-45, end=45, fill=C_WHITE, width=4)
        
    # Laser watermark of brand sword on card surface
    sw_watermark = get_clean_sword(target_h=260, color=(50, 62, 78, 255))
    im.alpha_composite(sw_watermark, (370 - sw_watermark.width // 2, 170))
    
    # Embossed typography
    f_num = get_font(30)
    d.text((140, 350), "•••• 001", font=f_num, fill=C_WHITE, anchor="lm")
    f_holder = get_font(22)
    d.text((140, 410), "СИЛА И ЧЕСТЬ", font=f_holder, fill=C_MUTED, anchor="lm")
    
    # Red security hologram badge
    d.rectangle([420, 390, 475, 430], fill=C_RED)
    
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
    d.arc([235, 96, 365, 130], start=0, end=180, fill=C_WHITE, width=8)
    
    # Circular neck brand mark at the nape
    d.ellipse([284, 140, 316, 172], fill=C_RED, outline=C_WHITE, width=3)
    
    # Full spine distressed sword (signature design of the brand's drop)
    # Extract authentic sword sized to span from collar to hem (height 340)
    sw_back = get_clean_sword(target_h=335, color=C_WHITE)
    im.alpha_composite(sw_back, (300 - sw_back.width // 2, 178))
    
    # Crimson spine fuller line
    d.line([(300, 215), (300, 490)], fill=C_RED, width=8)
    
    # Distressed cutouts / tactical battle scratches across blade
    for sy in [250, 310, 370, 430]:
        d.line([(282, sy), (294, sy + 6)], fill=C_CARBON, width=5)
        d.line([(318, sy + 12), (306, sy + 18)], fill=C_CARBON, width=5)
        
    finalize(im, "10_lookbook.png")

# =============================================================
# 11. CHANNEL — Канал бренда (Тактическая радиовышка / Сигнал)
# =============================================================
def make_channel():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Military broadcast tower with brand emblem & concentric signal waves
    # Tower legs (A-frame lattice)
    d.polygon([(290, 160), (310, 160), (410, 530), (370, 530)], fill=C_WHITE)
    d.polygon([(290, 160), (310, 160), (230, 530), (190, 530)], fill=C_WHITE)
    # Cross struts
    for y in [240, 320, 400, 480]:
        d.line([(240 + (y-160)*0.25, y), (360 - (y-160)*0.25, y)], fill=C_WHITE, width=8)
        
    # Central mast
    d.line([(300, 80), (300, 300)], fill=C_WHITE, width=12)
    # Top transmitter beacon
    d.ellipse([280, 60, 320, 100], fill=C_RED, outline=C_WHITE, width=6)
    
    # Center brand seal mounted on tower
    seal_mini = get_clean_seal(size=140, color=C_WHITE)
    im.alpha_composite(seal_mini, (300 - 70, 300))
    
    # Broadcast signal waves expanding from the beacon
    for r, col, w in [(90, C_WHITE, 8), (140, C_RED, 10), (190, C_WHITE, 12)]:
        d.arc([300 - r, 80 - r, 300 + r, 80 + r], start=210, end=330, fill=col, width=w)
        
    finalize(im, "11_channel.png")

# =============================================================
# 12. BACK — Шеврон возврата / Главное меню
# =============================================================
def make_back():
    im = make_canvas()
    d = ImageDraw.Draw(im)
    
    # Double tactical chevron pointing left («), matching the blade guard angle
    def draw_chevron(offset_x, fill_col, border_col, red_center=False):
        # High precision polygon for chamfered military chevron
        points = [
            (290 + offset_x, 100), (350 + offset_x, 100),
            (190 + offset_x, 300), (350 + offset_x, 500),
            (290 + offset_x, 500), (130 + offset_x, 300)
        ]
        d.polygon(points, fill=fill_col, outline=border_col)
        if red_center:
            # Inner crimson stripe
            d.polygon([
                (290 + offset_x, 140), (320 + offset_x, 140),
                (200 + offset_x, 300), (320 + offset_x, 460),
                (290 + offset_x, 460), (170 + offset_x, 300)
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
    grid_path = os.path.join(REPO_DIR, "custom_emoji_pack/preview_grid.png")
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
        ("07_cart.png", "07. КОФР ЗАКАЗА"),
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
        
        # Cell frame with beveled tactical style
        d.rounded_rectangle([x + 8, y + 8, x + cell_w - 8, y + cell_h - 8], radius=16, fill=(20, 25, 32, 255), outline=C_BORDER, width=2)
        
        # Load generated 100x100 icon, display at 88x88
        icon_path = os.path.join(OUT_DIR, fname)
        if os.path.exists(icon_path):
            icon = Image.open(icon_path).resize((88, 88), Image.Resampling.LANCZOS)
            bg.alpha_composite(icon, (x + (cell_w - 88) // 2, y + 22))
            
        # Label
        d.text((x + cell_w // 2, y + cell_h - 26), title, font=f_lbl, fill=C_MUTED, anchor="mm")
        
    bg.save(grid_path, "PNG")
    print(f"Generated preview grid: {grid_path}")

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
    print("All icons and preview grid successfully generated!")
