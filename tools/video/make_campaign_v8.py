#!/usr/bin/env python3
"""Кампания v8: монтаж с титрами для mute-просмотра и финалом-призывом.

Что изменено относительно v7 (по указаниям владельца):
1. Открытие без портрета: лицо убрано из начала фильма — вместо него
   типографская карточка «ВОРОЖБИТОВ / НОВЫЙ ВЫПУСК» (30 кадров).
2. Подъём получает рампу скорости (1.25x в середине) — кадр дышит.
3. Склейки — мягкие диссольвы 0.1 с, перед финалом dip-to-black 0.27 с.
4. Burn-in титры на каждом товарном плане (факты только из каталога:
   тираж, цена, принт, нашивка, жетон, размеры бегущей строкой).
5. Финал дополнен CTA-плашкой «Открыть витрину →».
6. Звук: кусок музыки мастера с конца (финал совпадает), дакинг 0.8 под
   финальные строки, розовый райзер 0.5 с перед финалом, loudnorm −16.
7. Дополнительно: шестисекундный крой cut6 и постеры -print/-jeton.

Кадры мастера режутся через select + setpts=N/30/TB: у orig-v6 после
склеек неровные pts, trim даёт неверные счётчики, а rawvideo-мультиплексор
без явного -r режет поток до 25 fps.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from make_video_v4 import FPS, Film, IVORY, encode_frames, span

FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())
FONT_DIR = Path(__file__).with_name("fonts")
WIDTH, HEIGHT = 720, 1280
FRAME_BYTES = WIDTH * HEIGHT * 3
STEEL = (150, 164, 180)
DIM = (168, 168, 162)
GRADE = "unsharp=5:5:0.35:5:5:0.0,eq=contrast=1.02:brightness=0.002:saturation=1.07"

# Границы планов в кадрах мастера (лицевой портрет 0–41 исключён).
PULLUP = (42, 76, 96, 144)   # a, mid0, mid1, b — середина ускоряется 1.25x
RAMP = 1.25
INTRO_FRAMES = 30
SIG_FRAMES = 194
DISSOLVE = 0.1
FADEBLACK = 0.27
NAME = "campaign-v8"


def run(*args) -> None:
    subprocess.run([str(a) for a in args], check=True)


def duration_of(path: Path) -> float:
    info = subprocess.run([str(FFMPEG), "-i", str(path)],
                          capture_output=True, text=True).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info)
    if not match:
        raise ValueError(f"cannot probe {path}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def oswald(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / "Oswald-Regular.ttf"), size)


def caption(image: Image.Image, text: str, t: float, *, y: int, size: int = 34,
            color=IVORY, tracking: float = 2.) -> None:
    """Центрированный титр с раскрытием за 0.25 с."""
    alpha = min(1., max(0., (t - .1) / .25))
    if alpha <= 0 or not text:
        return
    font = oswald(size)
    width = font.getlength(text) + tracking * (len(text) - 1)
    x = (WIDTH - width) / 2
    draw = ImageDraw.Draw(image, "RGBA")
    fill = color + (round(255 * alpha),)
    for ch in text:
        draw.text((round(x), y), ch, font=font, fill=fill, anchor="lt")
        x += font.getlength(ch) + tracking


def marquee(image: Image.Image, text: str, t: float, *, y: int = 1130) -> None:
    """Бегущая строка размеров."""
    font = oswald(30)
    tw = font.getlength(text)
    x = (t * 120) % (WIDTH + tw) - tw
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text((round(x), y), text, font=font, fill=STEEL + (235,), anchor="lt")


def render_segment(master: Path, out_path: Path, *, name: str, start: int, end: int,
                   ramp: tuple[int, int] | None = None, overlays=None) -> int:
    """Вырезать [start, end) из мастера, наложить титры в PIL, закодировать.

    Возвращает число кадров. PTS перенумеровываются (N/30/TB), fps=30
    фиксирует счётчик кадров для рампы и сырого конвейера.
    """
    if ramp is not None:
        mid0, mid1 = ramp
        graph = (
            f"[0:v]split=3[s1][s2][s3];"
            f"[s1]select='between(n,{start},{mid0 - 1})',setpts=N/{FPS}/TB[p1];"
            f"[s2]select='between(n,{mid0},{mid1 - 1})',setpts=N/({FPS}*{RAMP})/TB[p2];"
            f"[s3]select='between(n,{mid1},{end - 1})',setpts=N/{FPS}/TB[p3];"
            f"[p1][p2][p3]concat=n=3:v=1:a=0,fps={FPS},{GRADE},format=rgb24[out]"
        )
        expected = (mid0 - start) + round((mid1 - mid0) / RAMP) + (end - mid1)
    else:
        graph = (
            f"[0:v]select='between(n,{start},{end - 1})',setpts=N/{FPS}/TB,"
            f"fps={FPS},{GRADE},format=rgb24[out]"
        )
        expected = end - start
    decode = subprocess.Popen(
        [str(FFMPEG), "-nostdin", "-v", "error", "-threads", "1", "-i", str(master),
         "-filter_complex", graph, "-map", "[out]", "-r", str(FPS),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    encode = subprocess.Popen(
        [str(FFMPEG), "-nostdin", "-v", "error", "-y", "-threads", "1",
         "-f", "rawvideo", "-pixel_format", "rgb24",
         "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS), "-i", "pipe:0",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-an", str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert decode.stdout is not None and encode.stdin is not None
    index = 0
    try:
        while True:
            data = decode.stdout.read(FRAME_BYTES)
            if not data:
                break
            if len(data) != FRAME_BYTES:
                raise ValueError(f"{name}: коротки кадр {index}")
            image = Image.frombytes("RGB", (WIDTH, HEIGHT), data)
            t = index / FPS
            for overlay in (overlays or ()):
                overlay(image, t)
            encode.stdin.write(image.tobytes())
            index += 1
    finally:
        decode.stdout.close()
        encode.stdin.close()
    if decode.wait() != 0 or encode.wait() != 0:
        raise RuntimeError(f"{name}: ffmpeg failed")
    if index != expected:
        raise ValueError(f"{name}: кадров {index}, ждали {expected}")
    return index


class IntroCard(Film):
    """Открытие без лица: бренд и строка выпуска."""

    def frame_at(self, local_frame: int) -> Image.Image:
        t = local_frame / FPS
        frame = self.background.copy()
        self.particles(frame, t, .08)
        fade = min(1., t / .3)
        brand = span(t, .10, .70) * fade
        self.text(frame, "ВОРОЖБИТОВ", (360, 566), 54, display=True,
                  align="center", max_width=620, alpha=brand, tracking=2.)
        self.line(frame, (252, 654, 468, 654), alpha=.35 * brand, width=1)
        over = span(t, .45, 1.0) * fade
        self.text(frame, self.overline, (360, 676), 30, align="center",
                  max_width=600, alpha=over, color=STEEL, tracking=6.)
        return frame


class FinalCard(Film):
    """Финал: бренд, дроп, строка-призыв и CTA-плашка."""

    def signature(self, local_frame: int) -> Image.Image:
        t = local_frame / FPS
        frame = self.background.copy()
        self.particles(frame, t, .10)
        frame = self.emblem(frame, t)
        brand = span(t, .95, 1.55)
        self.text(frame, "СИЛА И ЧЕСТЬ", (360, 858 - 14 * (1 - brand)), 84,
                  display=True, align="center", max_width=620, alpha=brand, tracking=2.)
        drop = span(t, 1.9, 2.5)
        self.text(frame, "НОВЫЙ ДРОП УЖЕ В ВИТРИНЕ", (360, 972), 40,
                  align="center", max_width=600, alpha=drop, color=STEEL, tracking=3.)
        cta = span(t, 2.6, 3.2)
        self.text(frame, "Тираж маленький — смотри в боте", (360, 1042), 30,
                  align="center", max_width=560, alpha=cta, color=DIM)
        plaque = span(t, 3.3, 3.9)
        if plaque > 0:
            layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            draw.rounded_rectangle((188, 1108, 532, 1180), radius=16,
                                   fill=(18, 21, 24, round(200 * plaque)),
                                   outline=STEEL + (round(255 * plaque),), width=2)
            font = oswald(34)
            label = "Открыть витрину"
            arrow_len, gap, y_mid = 40., 16., 1144.
            tw = font.getlength(label)
            x0 = 360 - (tw + gap + arrow_len) / 2
            ink = IVORY + (round(255 * plaque),)
            draw.text((round(x0), round(y_mid)), label, font=font, anchor="lm",
                      fill=ink)
            ax0, ax1 = x0 + tw + gap, x0 + tw + gap + arrow_len
            draw.line((ax0, y_mid, ax1, y_mid), fill=ink, width=3)
            draw.line((ax1 - 10, y_mid - 9, ax1, y_mid), fill=ink, width=3)
            draw.line((ax1 - 10, y_mid + 9, ax1, y_mid), fill=ink, width=3)
            frame = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
        return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True,
                        help="мастер v6 (surgery/orig-v6.mp4)")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    if not args.master.is_file():
        raise FileNotFoundError(args.master)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    brand = catalog.get("brand", {})
    drop_word = str(brand.get("drop", "ВЫПУСК")).upper()
    overline = f"НОВЫЙ {drop_word}" if drop_word == "ВЫПУСК" else drop_word
    tee = next(p for p in catalog.get("products", [])
               if p.get("id") == "tee-sila-i-chest")
    price = str(tee.get("price", "4 900 ₽"))
    sizes = " · ".join(str(s) for s in tee.get("sizes", []))

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".campaign-v8-", dir=out))
    try:
        design = IntroCard(WIDTH)
        design.overline = overline
        intro = stage / "seg0.mp4"
        encode_frames((design.frame_at(i) for i in range(INTRO_FRAMES)),
                      (WIDTH, HEIGHT), INTRO_FRAMES, intro, str(FFMPEG), crf="19")

        specs = [
            ("seg1", PULLUP[0], PULLUP[3], (PULLUP[1], PULLUP[2]), ()),
            ("seg2", 144, 192, None,
             (lambda im, t: caption(im, f"Первый тираж · {price}", t, y=1090,
                                    color=STEEL),)),
            ("seg3", 192, 234, None,
             (lambda im, t: caption(im, "Фирменный принт", t, y=1090),)),
            ("seg4", 234, 297, None,
             (lambda im, t: caption(im, "Нашивка у подола", t, y=1090),)),
            ("seg5", 297, 317, None,
             (lambda im, t: caption(im, "Чёрная. Первый тираж.", t, y=1090,
                                    color=DIM),)),
            ("seg6", 317, 350, None,
             (lambda im, t: caption(im, "Жетон — из витрины", t, y=1090,
                                    size=30, color=DIM),)),
            ("seg7", 350, 403, None,
             (lambda im, t: marquee(im, sizes, t),)),
        ]
        paths = [intro]
        counts = [INTRO_FRAMES]
        for name, start, end, ramp, overlays in specs:
            path = stage / f"{name}.mp4"
            counts.append(render_segment(args.master, path, name=name,
                                         start=start, end=end, ramp=ramp,
                                         overlays=overlays))
            paths.append(path)
            print(f"{name}: {counts[-1]} кадров", flush=True)

        finale = FinalCard(WIDTH)
        sig = stage / "seg8.mp4"
        encode_frames((finale.signature(i) for i in range(SIG_FRAMES)),
                      (WIDTH, HEIGHT), SIG_FRAMES, sig, str(FFMPEG), crf="19")
        paths.append(sig)
        counts.append(SIG_FRAMES)

        # Диссольвы 0.1 с между планами, перед финалом dip-to-black 0.27 с.
        durations = [c / FPS for c in counts]
        xs = [DISSOLVE] * (len(paths) - 2) + [FADEBLACK]
        chain, off = [], 0.0
        prev = "0:v"
        for i, x in enumerate(xs, start=1):
            off += durations[i - 1] - x
            kind = "fadeblack" if x == FADEBLACK else "fade"
            tag = f"[v{i}]"
            chain.append(f"[{prev}][{i}:v]xfade=transition={kind}:"
                         f"duration={x:.3f}:offset={off:.4f}{tag}")
            prev = f"v{i}"
        total = sum(durations) - sum(xs)
        picture = stage / "picture.mp4"
        run(FFMPEG, "-nostdin", "-v", "error", "-y", "-threads", "1",
            *[a for p in paths for a in ("-i", p)],
            "-filter_complex", ";".join(chain),
            "-map", f"[{prev}]", "-an", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-pix_fmt", "yuv420p", picture)

        # Звук: хвост музыки мастера, дакинг под финал, райзер, loudnorm.
        master_dur = duration_of(args.master)
        a_off = max(0., master_dur - total)
        sig_start = total - SIG_FRAMES / FPS
        duck_a, duck_b = sig_start + .8, sig_start + 4.
        riser_at = max(0., sig_start - .5)
        audio = stage / "audio.m4a"
        run(FFMPEG, "-nostdin", "-v", "error", "-y",
            "-i", args.master,
            "-f", "lavfi", "-i",
            f"anoisesrc=color=pink:sample_rate=48000:duration=0.5:amplitude=0.16",
            "-filter_complex",
            f"[0:a]atrim=start={a_off:.3f}:end={a_off + total:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"volume=0.8:enable='between(t,{duck_a:.3f},{duck_b:.3f})'[a0];"
            f"[1:a]afade=t=in:st=0:d=0.5,afade=t=out:st=0.42:d=0.08,"
            f"adelay={int(riser_at * 1000)}|{int(riser_at * 1000)}[a1];"
            f"[a0][a1]amix=inputs=2:duration=first:normalize=0,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[out]",
            "-map", "[out]", "-t", f"{total:.3f}",
            "-c:a", "aac", "-b:a", "128k", audio)

        final = out / f"{NAME}.mp4"
        run(FFMPEG, "-nostdin", "-v", "error", "-y",
            "-i", picture, "-i", audio,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
            "-t", f"{total:.3f}", "-movflags", "+faststart", final)

        got = duration_of(final)
        if abs(got - total) > .15:
            raise ValueError(f"duration drift: {got} vs {total:.2f}")
        size = final.stat().st_size
        if size >= 6_000_000:
            raise ValueError(f"teaser too big: {size} bytes")
        print(f"готово: {final} · {got:.2f} с · {size / 1e6:.2f} МБ", flush=True)

        # Таймлайн для постеров: начало seg2 и seg7.
        t_print = durations[0] - xs[0] + durations[1] - xs[1] + .8
        t_start7 = t_print
        for i in (2, 3, 4, 5, 6):
            t_start7 += durations[i] - xs[i]
        t_jet = t_start7 + .6
        for suffix, width, at in [(".jpg", 720, .45), ("-thumb.jpg", 180, .45),
                                  ("-end.jpg", 720, got - .1),
                                  ("-print.jpg", 720, t_print),
                                  ("-jeton.jpg", 720, t_jet)]:
            run(FFMPEG, "-nostdin", "-v", "error", "-ss", f"{at:.2f}", "-i", final,
                "-frames:v", "1", "-vf", f"scale={width}:-2", "-y",
                out / f"{NAME}{suffix}")

        cut6 = out / f"{NAME}-cut6.mp4"
        run(FFMPEG, "-nostdin", "-v", "error", "-y",
            "-ss", f"{got - 6:.3f}", "-i", final, "-t", "6",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", cut6)
        print(f"cut6: {cut6} · {cut6.stat().st_size / 1e6:.2f} МБ", flush=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
