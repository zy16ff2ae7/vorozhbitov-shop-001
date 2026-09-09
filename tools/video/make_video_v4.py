#!/usr/bin/env python3
"""Director's cut: photographic chapters and an animated brand-signature finale.

Pillow draws at native master resolution. Photography is never deformed; all
movement is framing, light and typography. The source film remains untouched.
Outputs are staged and validated before any application asset is replaced.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from make_video_v3_corrected import has_faststart, probe

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "miniapp" / "assets"
FONT_DIR = Path(__file__).with_name("fonts")
FPS = 30
TOTAL_FRAMES = 936
WELCOME_FRAMES = 240
BLACK = (7, 8, 9)
IVORY = (233, 226, 208)
MUTED = (139, 140, 138)
RED = (147, 40, 35)


@dataclass(frozen=True)
class Scene:
    name: str
    frames: int
    file: str = ""
    title: str = ""
    note: str = ""
    fit: str = "contain"


SCENES = (
    Scene("trace", 78),
    Scene("night", 96, "drop/tee-night-v7.jpg", "СВОЙ ПУТЬ", "ГОРОД ПОСЛЕ ДОЖДЯ", "cover"),
    Scene("roof", 84, "drop/tee-roof-v3.jpg", "ХАРАКТЕР", "КРЫШИ / ПОСЛЕ ДОЖДЯ"),
    Scene("gym", 84, "drop/tee-gym-v3.jpg", "ДИСЦИПЛИНА", "КАЖДЫЙ ДЕНЬ"),
    Scene("back", 66, "drop/tee-back-v3.jpg", "СИЛА В ДЕЙСТВИИ", "ДЕРЖИ СВОЮ ЛИНИЮ"),
    Scene("detail", 102, "drop/tee-detail-v4.png", "ВЕЩЬ. В ДЕТАЛЯХ.", "ФАКТУРА / ЗНАК / КРОЙ"),
    Scene("crew", 90, "drop/tee-crew-v3.jpg", "СВОИ РЯДОМ", "ОДИН ВЫБОР. ОДИН ПУТЬ."),
    Scene("hero", 120, "drop/tee-night-v7.jpg", "СИЛА И ЧЕСТЬ", "ВОРОЖБИТОВ / ВЫПУСК 001", "cover"),
    Scene("signature", 216),
)
assert sum(s.frames for s in SCENES) == TOTAL_FRAMES


def smooth(value: float) -> float:
    p = max(0.0, min(1.0, value))
    return p * p * (3 - 2 * p)


def span(progress: float, lo: float, hi: float) -> float:
    return smooth((progress - lo) / (hi - lo))


class Film:
    def __init__(self, width: int = 1080, assets: Path = ASSETS, fonts: Path = FONT_DIR):
        self.width = width
        self.height = width * 16 // 9
        self.scale = width / 720
        self.assets, self.fonts = assets, fonts
        self.photos: dict[str, Image.Image] = {}
        self.background = self.make_background()
        self.shadow = self.make_shadow()
        self.mark = self.load_mark()

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def box(self, values):
        return tuple(self.px(v) for v in values)

    @lru_cache(maxsize=80)
    def font(self, size: int, display: bool = False):
        name = "RuslanDisplay.ttf" if display else "Oswald-Regular.ttf"
        return ImageFont.truetype(str(self.fonts / name), self.px(size))

    def make_background(self) -> Image.Image:
        # A static, very quiet material field; no full-frame random flicker.
        y, x = np.mgrid[0:self.height, 0:self.width].astype(np.float32)
        radius = ((x / self.width - .5) / .6) ** 2 + ((y / self.height - .43) / .64) ** 2
        glow = np.maximum(0, 1 - radius) ** 2
        noise = np.random.default_rng(41).normal(0, .45, glow.shape)
        base = np.stack([7 + glow * 9, 8 + glow * 11, 9 + glow * 13], axis=-1)
        base += noise[..., None]
        return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))

    def make_shadow(self) -> Image.Image:
        y = np.linspace(0, 1, self.height)
        alpha = np.maximum(np.clip((.18 - y) / .18, 0, 1) * .7,
                           np.clip((y - .60) / .40, 0, 1) ** 1.35 * .94)
        data = np.broadcast_to((alpha[:, None] * 255).astype(np.uint8), (self.height, self.width))
        layer = Image.new("RGBA", (self.width, self.height), BLACK + (0,))
        layer.putalpha(Image.fromarray(data.copy()))
        return layer

    def load_mark(self) -> Image.Image:
        path = self.assets / "brand/monogram-v4.png"
        with Image.open(path) as image:
            source = image.convert("RGBA")
        alpha = source.getchannel("A")
        if alpha.getextrema()[0] == 255:
            # The generator delivered ivory artwork on neutral gray instead of
            # alpha. Key it only in the video compositor, using its warm chroma;
            # the original RGB reference remains untouched on disk.
            rgb = np.asarray(source, dtype=np.float32)[..., :3]
            warm = np.clip((rgb[..., 0] - rgb[..., 2] - 10) / 7, 0, 1)
            bright = np.clip((rgb.mean(axis=2) - 205) / 20, 0, 1)
            matte = (warm * bright * 255).astype(np.uint8)
            fraction = np.count_nonzero(matte > 200) / matte.size
            if not .03 < fraction < .65:
                raise ValueError("Emblem matte is empty or ambiguous")
            alpha = Image.fromarray(matte)
            source.putalpha(alpha)
        bounds = alpha.getbbox()
        if bounds is None:
            raise ValueError("The brand emblem is empty")
        # Only the compositor's active area is used; the source asset is unmodified.
        source = source.crop(bounds)
        factor = min(self.px(302) / source.width, self.px(554) / source.height)
        return source.resize((round(source.width * factor), round(source.height * factor)), Image.Resampling.LANCZOS)

    def photo(self, name: str) -> Image.Image:
        if name not in self.photos:
            with Image.open(self.assets / name) as im:
                im = im.convert("RGB")
            # Restrained video grade, cached once per source photograph.
            im = ImageEnhance.Color(im).enhance(.88)
            self.photos[name] = ImageEnhance.Contrast(im).enhance(1.035)
        return self.photos[name]

    def text(self, frame, value, xy, size=22, *, display=False, alpha=1., color=IVORY,
             tracking=0., align="left", max_width=None):
        if not value or alpha <= 0:
            return
        while True:
            font = self.font(size, display)
            width = font.getlength(value) + self.px(tracking) * (len(value) - 1)
            if max_width is None or width <= self.px(max_width) or size <= 10:
                break
            size -= 1
        x, y = self.box(xy)
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        draw = ImageDraw.Draw(frame, "RGBA")
        fill = color + (round(255 * min(1., alpha)),)
        for ch in value:
            draw.text((round(x), y), ch, font=font, fill=fill, anchor="lt")
            x += font.getlength(ch) + self.px(tracking)

    def line(self, frame, points, color=IVORY, alpha=.4, width=1):
        draw = ImageDraw.Draw(frame, "RGBA")
        draw.line(self.box(points), fill=color + (round(255 * max(0, min(1, alpha))),), width=max(1, self.px(width)))

    def place_photo(self, frame, source, bounds, *, contain=False, zoom=1., focus=(.5, .45)):
        x, y, w, h = self.box(bounds)
        if contain:
            # Gentle movement lives inside a fixed safe area. Full artwork and
            # gym props stay visible, including the suspended feet in BACK.
            factor = min(w / source.width, h / source.height) * min(1., zoom)
            sw, sh = round(source.width * factor), round(source.height * factor)
            fitted = source.resize((sw, sh), Image.Resampling.LANCZOS)
            frame.paste(fitted, (x + (w - sw) // 2, y + (h - sh) // 2))
        else:
            factor = max(w / source.width, h / source.height) * zoom
            cw, ch = w / factor, h / factor
            left = max(0., min(source.width - cw, source.width * focus[0] - cw / 2))
            top = max(0., min(source.height - ch, source.height * focus[1] - ch / 2))
            fitted = source.resize((w, h), Image.Resampling.LANCZOS, box=(left, top, left + cw, top + ch))
            frame.paste(fitted, (x, y))

    def particles(self, frame, t, strength=.15):
        draw = ImageDraw.Draw(frame, "RGBA")
        for i in range(18):
            x = 72 + ((i * 83.31 + math.sin(t * .22 + i) * 13) % 576)
            y = 198 + ((i * 67.19 - t * (3 + i % 4)) % 802)
            a = strength * (.4 + .6 * math.sin(i * 2.13 + t * .6) ** 2)
            r = .55 + .45 * (i % 3) / 2
            draw.ellipse(self.box((x-r, y-r, x+r, y+r)), fill=IVORY + (round(a * 255),))

    def chapter(self, scene: Scene, local_frame: int, chapter: int) -> Image.Image:
        t = local_frame / FPS
        p = local_frame / max(1, scene.frames - 1)
        frame = self.background.copy()
        image = self.photo(scene.file)
        if scene.fit == "cover":
            self.place_photo(frame, image, (0, 0, 720, 1280), zoom=1. + .032 * smooth(p), focus=(.5, .43))
            frame = Image.alpha_composite(frame.convert("RGBA"), self.shadow).convert("RGB")
        else:
            if scene.name == "back":
                bounds = (24, 116, 672, 974)
            elif scene.name == "detail":
                bounds = (0, 218, 720, 784)
            else:
                bounds = (24, 218, 672, 766)
            is_detail = scene.name == "detail"
            self.place_photo(frame, image, bounds, contain=not is_detail,
                             zoom=1.01+.02*smooth(p) if is_detail else .975+.025*smooth(p))
            self.line(frame, (24, 196, 83, 196), RED, .92, 2)
            self.line(frame, (637, 1006, 696, 1006), IVORY, .4)
        self.text(frame, "ВОРОЖБИТОВ", (32, 49), 17, tracking=3)
        self.text(frame, "ВЫПУСК 001", (688, 51), 13, color=MUTED, tracking=2, align="right")
        a = smooth((t - .14) / .48)
        self.text(frame, f"0{chapter} / {scene.note}", (34, 1072), 14, color=MUTED,
                  tracking=1.6, alpha=a, max_width=652)
        title_y = 1122 + (1 - a) * 18
        self.text(frame, scene.title, (32, title_y), 65 if scene.name != "hero" else 72,
                  display=True, alpha=a, max_width=656)
        self.line(frame, (34, 1230, 686, 1230), MUTED, .21)
        self.line(frame, (34, 1230, 34 + 652 * p, 1230), RED, .9, 2)
        # Brief exposure shutter at cuts: dark, never a full-screen white flash.
        in_a = smooth(t / .10)
        out_a = smooth((scene.frames / FPS - t) / .12)
        if in_a * out_a < 1:
            frame = Image.blend(Image.new("RGB", frame.size, BLACK), frame, in_a * out_a)
        return frame

    def emblem(self, frame, t, *, reveal=True):
        mark = self.mark
        x = (self.width - mark.width) // 2
        y = self.px(263)
        # Animate the lighting and reveal mask in the video compositor. No new
        # contours are invented; the supplied mark stays geometrically rigid.
        reveal_p = span(t, .38, 2.15) if reveal else 1.
        yy, xx = np.mgrid[:mark.height, :mark.width].astype(np.float32)
        reveal_mask = np.clip((reveal_p * (mark.height + self.px(36)) - yy) / self.px(36), 0, 1)
        a = np.asarray(mark.getchannel("A"), dtype=np.float32) / 255 * reveal_mask
        sweep_y = (t - 2.25) / 1.15 * mark.height
        shine = np.exp(-((yy - sweep_y + (xx - mark.width / 2) * .35) / self.px(29)) ** 2)
        edge = .8 + .12 * np.cos(xx / max(1, mark.width) * math.pi * 4)
        color = np.zeros((mark.height, mark.width, 4), dtype=np.uint8)
        for c, base in enumerate((181, 189, 194)):
            color[..., c] = np.clip(base * edge + shine * (255-base), 0, 255)
        color[..., 3] = np.clip(a * 255 * smooth(t / .35), 0, 255)
        surface = Image.fromarray(color)
        # The warm flare travels only along the active reveal edge.
        glow_alpha = np.clip(a * np.exp(-((yy - reveal_p * mark.height) / self.px(30)) ** 2) * 140, 0, 255).astype(np.uint8)
        glow = Image.new("RGBA", mark.size, (225, 203, 169, 0))
        glow.putalpha(Image.fromarray(glow_alpha))
        glow = glow.filter(ImageFilter.GaussianBlur(self.px(9)))
        rgba = frame.convert("RGBA")
        rgba.alpha_composite(glow, (x, y))
        rgba.alpha_composite(surface, (x, y))
        return rgba.convert("RGB")

    def signature(self, local_frame: int) -> Image.Image:
        t = local_frame / FPS
        frame = self.background.copy()
        self.particles(frame, t, .17)
        self.text(frame, "ВЫПУСК 001", (360, 112), 16, tracking=4, align="center", color=MUTED,
                  alpha=smooth(t / .6))
        frame = self.emblem(frame, t)
        a = span(t, 2.75, 3.40)
        self.text(frame, "СИЛА И ЧЕСТЬ", (360, 905 + 16 * (1-a)), 72, display=True,
                  align="center", max_width=640, alpha=a)
        b = span(t, 3.35, 4.15)
        self.line(frame, (234, 1022, 486, 1022), IVORY, b * .35)
        self.text(frame, "ВОРОЖБИТОВ", (360, 1060), 24, tracking=5,
                  align="center", alpha=b)
        self.text(frame, "1993 / ВЫПУСК 001", (360, 1130), 16, tracking=2.2,
                  color=MUTED, align="center", alpha=span(t, 4.1, 4.7), max_width=650)
        self.text(frame, "ТИРАЖ ОГРАНИЧЕН", (360, 1200), 14, tracking=3.5,
                  color=RED, align="center", alpha=span(t, 4.55, 5.1))
        fade = smooth((7.2 - t) / .65)
        return Image.blend(Image.new("RGB", frame.size, BLACK), frame, fade)

    def trace(self, local_frame: int) -> Image.Image:
        t = local_frame / FPS
        frame = self.background.copy()
        self.particles(frame, t, .1)
        p = span(t, .10, 1.45)
        tip_y = 400 + 326 * p
        a = smooth(t / .28) * (1 - span(t, 1.65, 2.35))
        self.line(frame, (360, 400, 360, tip_y), IVORY, .8 * a, 1)
        d = ImageDraw.Draw(frame, "RGBA")
        r = 3 * a
        d.ellipse(self.box((360-r, tip_y-r, 360+r, tip_y+r)), fill=(246, 231, 203, round(220*a)))
        self.text(frame, "ВОРОЖБИТОВ", (360, 794), 60, display=True, align="center",
                  max_width=632, alpha=span(t, .50, 1.10))
        self.text(frame, "СИЛА И ЧЕСТЬ / ВЫПУСК 001", (360, 890), 15,
                  tracking=2.8, align="center", color=MUTED, alpha=span(t, 1., 1.6))
        fade = smooth((2.6-t) / .18)
        return Image.blend(Image.new("RGB", frame.size, BLACK), frame, fade)

    def frame(self, frame_number: int) -> Image.Image:
        if not 0 <= frame_number < TOTAL_FRAMES:
            raise ValueError("Frame outside film timeline")
        start = 0
        for index, scene in enumerate(SCENES):
            if frame_number < start + scene.frames:
                local = frame_number - start
                if scene.name == "trace":
                    return self.trace(local)
                if scene.name == "signature":
                    return self.signature(local)
                return self.chapter(scene, local, index)
            start += scene.frames
        raise AssertionError("Unreachable timeline gap")

    def welcome(self, frame_number: int) -> Image.Image:
        t = (frame_number % WELCOME_FRAMES) / FPS
        w, h = 810, 888
        def fit(name, contain, zoom):
            source = self.photo(name)
            base = Image.new("RGB", (w, h), BLACK)
            scale = (min if contain else max)(w/source.width, h/source.height) * zoom
            if contain:
                size = (round(source.width*scale), round(source.height*scale))
                image = source.resize(size, Image.Resampling.LANCZOS)
                base.paste(image, ((w-size[0])//2, (h-size[1])//2))
            else:
                cw, ch = w/scale, h/scale
                left = (source.width-cw)/2
                top = max(0, min(source.height-ch, source.height*.43-ch/2))
                base = source.resize((w,h), Image.Resampling.LANCZOS, box=(left,top,left+cw,top+ch))
            return base
        wave_zoom = 1.02 + .008 * math.cos(2*math.pi*t/8)
        night = fit("drop/tee-night-v7.jpg", False, wave_zoom)
        back = fit("drop/tee-back-v3.jpg", True, .97 + .012 * math.cos(2*math.pi*t/8))
        amount = span(t, 2.1, 2.85) * (1-span(t, 5.6, 6.35))
        return Image.blend(night, back, amount)


def make_audio(original: Path, output: Path, ffmpeg: str):
    """Preserve the original first 24 seconds and score the expanded signature."""
    sr = 48000
    n = int(TOTAL_FRAMES / FPS * sr)
    decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(original), "-vn", "-ar", str(sr),
                              "-ac", "2", "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
    original_pcm = np.frombuffer(decoded, dtype="<f4").reshape(-1, 2)
    mix = np.zeros((n, 2), np.float32)
    count = min(n, len(original_pcm))
    time = np.arange(n) / sr
    # Keep the musical character; old final cues do not overlap the new reveal.
    envelope = 1 - np.clip((time[:count]-23.7)/.65, 0, 1)
    mix[:count] = original_pcm[:count] * envelope[:, None] * .9
    rng = np.random.default_rng(93)

    def place(signal, at, gain=1., pan=0.):
        i = round(at*sr)
        signal = signal[:max(0, n-i)]
        if not len(signal):
            return
        mix[i:i+len(signal),0] += signal*gain*(1-min(0.8,max(0.,pan)))
        mix[i:i+len(signal),1] += signal*gain*(1-min(0.8,max(0.,-pan)))

    def impact(duration=1.6):
        tt = np.arange(round(sr*duration))/sr
        f = 44 + 46*np.exp(-tt*18)
        phase = 2*np.pi*np.cumsum(f)/sr
        return (np.sin(phase)*np.exp(-tt*4.2) + .20*np.sin(phase*2.04)*np.exp(-tt*7)) * (1-np.exp(-tt*90))

    def steel(duration=2.2):
        tt = np.arange(round(sr*duration))/sr
        sound = np.zeros(len(tt))
        for freq, gain in ((1480,.12),(2287,.07),(3561,.035),(4920,.013)):
            sound += gain*np.sin(2*np.pi*freq*tt+rng.uniform(0,6.28))*np.exp(-tt*(2+freq/5000))
        sound *= (1-np.exp(-tt*80))
        return sound

    # Low D-minor resonance carries the longer, restrained ending.
    tt = np.arange(round(sr*7.4))/sr
    chord = sum(np.sin(2*np.pi*f*tt)*g for f,g in ((73.42,.085),(110,.058),(146.83,.025),(174.61,.018)))
    chord *= np.clip(tt/.8,0,1)*np.clip((7.4-tt)/1.6,0,1)
    place(chord, 23.8, 1.)
    place(steel(), 24.45, .85, -.25)
    place(steel(1.8), 26.25, .35, .2)
    place(impact(), 26.9, .42)
    # A small downward transient under the title, followed by space and decay.
    place(impact(1.1), 28.2, .15)
    mix *= np.clip(time/.12,0,1)[:,None] * np.clip((TOTAL_FRAMES/FPS-time)/.5,0,1)[:,None]
    peak = float(np.abs(mix).max())
    if peak > .87:
        mix *= .87/peak
    with wave.open(str(output), "wb") as wave_file:
        wave_file.setnchannels(2)
        wave_file.setsampwidth(2)
        wave_file.setframerate(sr)
        wave_file.writeframes((np.clip(mix,-1,1)*32767).astype("<i2").tobytes())


def encode_frames(frames, size, count, path, ffmpeg, audio=None, crf="18"):
    command = [ffmpeg,"-y","-v","warning","-threads","1","-f","rawvideo","-pixel_format","rgb24",
               "-video_size",f"{size[0]}x{size[1]}","-framerate",str(FPS),"-i","pipe:0"]
    if audio:
        command += ["-i",str(audio),"-map","0:v:0","-map","1:a:0","-c:a","aac","-b:a","192k",
                    "-af","loudnorm=I=-16:TP=-1.5:LRA=9","-ar","48000"]
    else:
        command += ["-an"]
    command += ["-c:v","libx264","-preset","medium","-crf",crf,"-threads","1",
                "-pix_fmt","yuv420p","-movflags","+faststart","-t",str(count/FPS),str(path)]
    with path.with_suffix(".log").open("w+") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log, stdout=subprocess.DEVNULL)
        try:
            assert process.stdin is not None
            written = 0
            for number, frame in enumerate(frames):
                if frame.size != size:
                    raise ValueError(f"Unexpected frame size: {frame.size}")
                process.stdin.write(frame.convert("RGB").tobytes())
                written += 1
                if (number+1)%90 == 0:
                    print(f"{path.name}: {number+1}/{count}", flush=True)
            if written != count:
                raise ValueError("Frame generator returned the wrong number of frames")
            process.stdin.close()
            if process.wait() != 0:
                log.seek(0)
                raise RuntimeError(log.read()[-3000:])
        except BaseException:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        process.kill()
                    process.wait()
            raise
        finally:
            if process.stdin and not process.stdin.closed:
                with suppress(OSError):
                    process.stdin.close()


def validate(path, size, frames, ffprobe, audio, max_bytes=None):
    info = probe(path, ffprobe)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1:
        raise ValueError("Expected one video stream")
    v = videos[0]
    if ((v["width"],v["height"]) != size or int(v["nb_frames"]) != frames or v["r_frame_rate"] != "30/1"
            or v["codec_name"] != "h264" or v["pix_fmt"] != "yuv420p"):
        raise ValueError(f"Invalid video format: {v}")
    if abs(float(v["duration"])-frames/FPS) > .01 or len(sounds) != int(audio):
        raise ValueError("Unexpected duration or audio stream count")
    if audio and sounds[0]["codec_name"] != "aac":
        raise ValueError("Expected AAC audio")
    if not has_faststart(path) or (max_bytes and path.stat().st_size >= max_bytes):
        raise ValueError("Video exceeds its size limit or lacks faststart")
    return {"file":path.name,"size":size,"frames":frames,"duration":float(v["duration"]),
            "bytes":path.stat().st_size,"audio":bool(sounds),"faststart":True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=ASSETS)
    parser.add_argument("--fonts", type=Path, default=FONT_DIR)
    parser.add_argument("--output", type=Path, required=True, help="Master and review deliverables directory")
    parser.add_argument("--previews", action="store_true", help="Render storyboard and validate inputs only")
    args = parser.parse_args()
    target = args.assets / "video"
    if args.output.resolve() == target.resolve():
        parser.error("--output must be separate from the application video directory")
    args.output.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        parser.error("ffmpeg and ffprobe must be installed")
    for path in [args.fonts/"RuslanDisplay.ttf",args.fonts/"Oswald-Regular.ttf",args.assets/"brand/monogram-v4.png"]:
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    for scene in SCENES:
        if scene.file and not (args.assets/scene.file).is_file():
            parser.error(f"Missing scene: {scene.file}")
    with suppress(OSError):
        os.nice(10)
    film = Film(1080, args.assets, args.fonts)
    storyboard = args.output/"storyboard"
    storyboard.mkdir(exist_ok=True)
    position = 0
    for scene in SCENES:
        local = min(scene.frames-1, 150 if scene.name == "signature" else max(24, scene.frames//2))
        film.frame(position+local).save(storyboard/f"{scene.name}.jpg", quality=94)
        position += scene.frames
    if args.previews:
        print("Storyboard ready; all source assets and fonts validated.")
        return
    target.mkdir(exist_ok=True)
    if args.assets.stat().st_dev != target.stat().st_dev:
        parser.error("Video staging and welcome image must be on the same filesystem")
    with tempfile.TemporaryDirectory(prefix=".video-v4-", dir=target) as scratch:
        stage = Path(scratch)
        audio = stage/"score.wav"
        make_audio(args.assets/"video/teaser.mp4", audio, ffmpeg)
        master = stage/"teaser-v4-master.mp4"
        encode_frames((film.frame(i) for i in range(TOTAL_FRAMES)), (1080,1920), TOTAL_FRAMES, master, ffmpeg, audio)
        records = [validate(master,(1080,1920),TOTAL_FRAMES,ffprobe,True)]
        mobile = stage/"teaser-v4.mp4"
        subprocess.run([ffmpeg,"-y","-v","warning","-threads","1","-i",str(master),
                        "-map","0:v:0","-map","0:a:0","-vf","scale=720:1280:flags=lanczos",
                        "-c:v","libx264","-crf","24","-preset","medium","-threads","1",
                        "-maxrate","1150k","-bufsize","2300k","-c:a","aac","-b:a","128k",
                        "-pix_fmt","yuv420p","-movflags","+faststart",str(mobile)],check=True)
        records.append(validate(mobile,(720,1280),TOTAL_FRAMES,ffprobe,True,6_000_000))
        welcome = stage/"welcome-loop-v4.mp4"
        encode_frames((film.welcome(i) for i in range(WELCOME_FRAMES)),(810,888),WELCOME_FRAMES,welcome,ffmpeg,crf="23")
        records.append(validate(welcome,(810,888),WELCOME_FRAMES,ffprobe,False,4_000_000))
        poster = film.frame(round(22.3*FPS))
        poster.resize((720,1280),Image.Resampling.LANCZOS).save(stage/"teaser-poster-v4.jpg",quality=92,optimize=True)
        poster.resize((180,320),Image.Resampling.LANCZOS).save(stage/"teaser-thumbnail-v4.jpg",quality=88,optimize=True)
        welcome_poster = film.welcome(0)
        welcome_poster.save(stage/"welcome-poster-v4.jpg",quality=93,optimize=True)
        shutil.copy2(stage/"welcome-poster-v4.jpg",stage/"welcome-v4.jpg")
        with Image.open(stage/"teaser-thumbnail-v4.jpg") as thumb:
            if thumb.size != (180,320) or (stage/"teaser-thumbnail-v4.jpg").stat().st_size > 200_000:
                raise ValueError("Invalid Telegram thumbnail")
        # Export complete, validated files. Versioned names keep prior cuts intact.
        shutil.copy2(master,args.output/master.name)
        shutil.copy2(mobile,args.output/mobile.name)
        shutil.copy2(welcome,args.output/welcome.name)
        for name in ("teaser-v4.mp4","welcome-loop-v4.mp4","teaser-poster-v4.jpg","teaser-thumbnail-v4.jpg","welcome-poster-v4.jpg"):
            os.replace(stage/name,target/name)
        os.replace(stage/"welcome-v4.jpg",args.assets/"welcome-v4.jpg")
        (args.output/"render-report.json").write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(json.dumps(records,ensure_ascii=False,indent=2),flush=True)


if __name__ == "__main__":
    main()
