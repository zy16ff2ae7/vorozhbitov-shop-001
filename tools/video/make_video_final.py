#!/usr/bin/env python3
"""Original 917-frame rhythm and sword finale with the final campaign photographs.

Usage: python3 tools/video/make_video_final.py --font-dir tools/video/fonts --teaser
The original teaser.mp4 supplies its AAC stream unchanged. Outputs are versioned.
"""
from pathlib import Path
import os
import tempfile
from PIL import Image, ImageDraw, ImageFilter
import make_video_v3_corrected as film
from make_video_v3_corrected import (
    np, CREAM, DIM, BLOOD, BLACK, W, H, IW, IH, IY, OUTRO_SEC, FPS,
    rgba, ease, ustav, caption, fit_ustav, draw_tracked, title_anim,
    Weave, film_grade, gate_mask,
)

def draw_sword(d: ImageDraw.ImageDraw, cx: float, top: float, length: float, progress: float, a: float,
               col=CREAM, lw: int = 3) -> None:
    """Меч остриём вниз, рисуется сверху вниз по progress 0..1 (как на спине футболки)."""
    pommel_y = top
    grip_top, grip_bot = top + 26, top + 96
    guard_y = grip_bot + 6
    blade_top, blade_bot = guard_y + 10, top + length
    fill = rgba(col, a)
    # прогресс: навершие → рукоять → гарда → клинок
    if progress > 0.0:
        d.ellipse((cx - 16, pommel_y - 16, cx + 16, pommel_y + 16), outline=fill, width=lw)
        d.ellipse((cx - 5, pommel_y - 5, cx + 5, pommel_y + 5), fill=fill)
    if progress > 0.08:
        y = grip_top + (grip_bot - grip_top) * min(1, (progress - 0.08) / 0.12)
        d.rectangle((cx - 9, grip_top, cx + 9, y), outline=fill, width=lw)
        for k in range(1, 5):
            yy = grip_top + k * 14
            if yy < y:
                d.line((cx - 9, yy, cx + 9, yy), fill=fill, width=2)
    if progress > 0.2:
        half = 78 * min(1, (progress - 0.2) / 0.1)
        d.rectangle((cx - half, guard_y - 8, cx + half, guard_y + 8), fill=fill)
    if progress > 0.3:
        pb = min(1, (progress - 0.3) / 0.7)
        y = blade_top + (blade_bot - blade_top) * pb
        wtop, wbot = 13.0, 3.5
        wy = wtop + (wbot - wtop) * pb
        pts = [(cx - wtop, blade_top), (cx + wtop, blade_top), (cx + wy, y - (26 if pb >= 1 else 0)), (cx, y), (cx - wy, y - (26 if pb >= 1 else 0))]
        d.polygon(pts, outline=fill, width=lw)
        d.line((cx, blade_top + 8, cx, y - 34), fill=rgba(col, a * 0.75), width=2)

def seg_outro(rng: np.random.Generator):
    n = int(OUTRO_SEC * FPS)
    weave = Weave(rng)
    for i in range(n):
        t = i / FPS
        dx, dy, fl = weave.step()
        base = np.full((IH, IW, 3), 0.035, dtype=np.float32)
        win = film_grade(Image.fromarray((base * 255).astype(np.uint8), "RGB"), rng, fl)
        frame = Image.new("RGB", (W, H), BLACK)
        frame.paste(win, (0, IY), gate_mask())
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, IY, IW - 1, IY + IH - 1), radius=22, outline=rgba(CREAM, 0.22), width=2)
        cx = W / 2 + dx * 0.5
        # монограмма ВВ с точками — как на спине
        a0 = ease((t - 0.1) / 0.3)
        if a0 > 0:
            f = ustav(64)
            d.text((cx, 560 + dy * 0.5), "ВВ", font=f, fill=rgba(CREAM, a0 * fl), anchor="ms")
            for sx in (-52, 52):
                d.ellipse((cx + sx - 4, 540 + dy * 0.5 - 4, cx + sx + 4, 540 + dy * 0.5 + 4), fill=rgba(CREAM, a0))
        # меч рисуется 0.25→1.35 с
        pr = ease((t - 0.25) / 1.1)
        if pr > 0:
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw_sword(ImageDraw.Draw(glow), cx, 610 + dy * 0.5, 560, pr, 0.8, col=(255, 225, 190), lw=7)
            layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)))
            d = ImageDraw.Draw(layer)
            draw_sword(d, cx, 610 + dy * 0.5, 560, pr, min(1.0, fl))
        # титр
        a1, s1 = title_anim(t, 1.5)
        if a1 > 0:
            size = int(fit_ustav("СИЛА И ЧЕСТЬ", 118, W - 140) * s1)
            tf = ustav(size)
            tw = tf.getlength("СИЛА И ЧЕСТЬ")
            d.text(((W - tw) / 2 + dx * 0.5 + 3, 1330 + 4), "СИЛА И ЧЕСТЬ", font=tf, fill=rgba((0, 0, 0), 0.8 * a1), anchor="ls")
            d.text(((W - tw) / 2 + dx * 0.5, 1330), "СИЛА И ЧЕСТЬ", font=tf, fill=rgba(CREAM, a1 * fl), anchor="ls")
        a2 = ease((t - 1.95) / 0.35)
        if a2 > 0:
            draw_tracked(d, W / 2, 1420, "ВОРОЖБИТОВ  ·  ДРОП 001  ·  2026", caption(28), rgba(DIM, a2), tr=0.32, align="center")
        a3 = ease((t - 2.3) / 0.35)
        if a3 > 0:
            draw_tracked(d, W / 2, 1560, "ТИРАЖ ОГРАНИЧЕН", caption(24), rgba(BLOOD, a3), tr=0.42, align="center")
        out = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
        if t > OUTRO_SEC - 0.5:  # затемнение
            k = 1 - (t - (OUTRO_SEC - 0.5)) / 0.5
            out = Image.fromarray((np.asarray(out, dtype=np.float32) * k).astype(np.uint8), "RGB")
        yield out

def configure():
    film.NIGHT = "campaign-final/night.jpg"
    film.CREW = "campaign-final/crew.jpg"
    film.ROOF = "campaign-final/roof.jpg"
    film.GYM = "drop/tee-gym-retouched-final.jpg"
    film.BACK = "drop/tee-back-final.jpg"
    dog, ring, forge = [f"campaign-final/{name}.jpg" for name in ("dog", "ring", "forge")]
    film.SHOTS = [
        (film.NIGHT, "НОЧЬ", 3.2, (.5,.45), "push"),
        (film.CREW, "СВОИ", 2.8, (.5,.45), "still"),
        (dog, "ВЕРНОСТЬ", 2.8, (.5,.45), "still"),
        (film.GYM, "ЖЕЛЕЗО", 2.2, (.5,.43), "punch"),
        (ring, "БОЙ", 1.8, (.5,.45), "punch"),
        (forge, "ХАРАКТЕР", 1.6, (.5,.45), "punch"),
        (film.BACK, "МЕЧ", 1.4, (.5,.4), "still"),
        (film.ROOF, "ГОРОД", 1.2, (.5,.45), "still"),
    ]
    film.SLIP_SRC = film.ROOF
    film.MONTAGE = [(name, (.5,.45)) for name in (film.CREW, ring, film.GYM, forge, dog, film.BACK)]
    film.HERO = (film.GYM, "СИЛА И ЧЕСТЬ", 3.6, (.5,.43), "push")
    film.seg_outro = seg_outro


def main():
    configure()
    config = film.arguments()
    if config.welcome:
        raise ValueError("Use --teaser; the silent welcome is encoded from the validated film.")
    if config.audio_source.resolve() == (config.output_dir / "teaser-final-30s.mp4").resolve():
        raise ValueError("Audio source must not be the final output file")
    film.validate_inputs(config)
    for name in {shot[0] for shot in film.SHOTS} | {film.HERO[0], film.SLIP_SRC, film.FLATLAY}:
        with Image.open(config.assets_dir / name) as image:
            image.verify()
    if film.plan()[2] != 917:
        raise ValueError("Timeline mismatch")
    if config.check_inputs:
        print("Validated final film: 917 frames")
        return
    os.nice(10)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    names = {"teaser-v3.mp4": "teaser-final-30s.mp4", "teaser-poster-v3.jpg": "teaser-final-30s.jpg", "teaser-thumbnail-v3.jpg": "teaser-final-30s-thumb.jpg"}
    with tempfile.TemporaryDirectory(prefix=".film-final-", dir=config.output_dir) as temporary:
        outputs = film.render_teaser(config, Path(temporary))
        for source, _ in outputs:
            destination = config.output_dir / names[source.name]
            source.replace(destination)
            print(f"Written: {destination}", flush=True)


if __name__ == "__main__":
    main()
