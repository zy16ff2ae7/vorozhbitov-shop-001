#!/usr/bin/env python3
"""Assemble three reviewed Wan scenes and a native-resolution brand signature.

No generated media is requested here. Inputs are local, reviewed 1080p clips.
The source photographs, logo, live catalog and production server are untouched.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
FONTS = Path(__file__).resolve().parent / "fonts"
W, H, FPS, DURATION = 1080, 1920, 30, 21
CREAM = (236, 228, 212)


def run(*args: str) -> None:
    subprocess.run([str(a) for a in args], check=True)


def smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def emblem() -> Image.Image:
    # Key the existing opaque artwork only inside the motion compositor.
    with Image.open(ROOT / "miniapp/assets/brand/monogram-v4.png") as source:
        rgb = np.asarray(source.convert("RGB"), dtype=np.float32)
    warm = np.clip((rgb[..., 0] - rgb[..., 2] - 10) / 7, 0, 1)
    bright = np.clip((rgb.mean(axis=2) - 205) / 20, 0, 1)
    matte = (warm * bright * 255).astype(np.uint8)
    if not .03 < np.count_nonzero(matte > 200) / matte.size < .65:
        raise ValueError("Ambiguous brand emblem matte")
    mark = Image.new("RGBA", (rgb.shape[1], rgb.shape[0]), CREAM + (0,))
    mark.putalpha(Image.fromarray(matte))
    box = mark.getbbox()
    if box is None:
        raise ValueError("Empty brand emblem")
    mark = mark.crop(box)
    return mark.resize((round(mark.width * 770 / mark.height), 770), Image.Resampling.LANCZOS)


def signature_frame(t: float, mark: Image.Image, background: Image.Image) -> Image.Image:
    frame = background.copy().convert("RGBA")
    layer = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(layer)
    appear = smooth(t / .6)
    d.rounded_rectangle((48, 180, W - 49, H - 181), radius=18,
                        outline=CREAM + (round(43 * appear),), width=1)
    d.text((W / 2, 272), "ВОРОЖБИТОВ", anchor="mm",
           font=ImageFont.truetype(str(FONTS / "Oswald-Regular.ttf"), 30),
           fill=CREAM + (round(155 * appear),))
    # The exact emblem is revealed along the blade; it never changes shape.
    reveal = smooth((t - .15) / 1.55)
    part = mark.copy()
    alpha = np.asarray(part.getchannel("A")).astype(np.float32)
    rows = np.arange(mark.height)[:, None]
    alpha *= np.clip((reveal * (mark.height + 100) - rows) / 100, 0, 1)
    part.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    layer.alpha_composite(part, ((W - mark.width) // 2, 398))
    title_alpha = smooth((t - 1.7) / .65)
    d = ImageDraw.Draw(layer)
    title_font = ImageFont.truetype(str(FONTS / "RuslanDisplay.ttf"), 112)
    d.text((W / 2, 1300 + 16 * (1 - title_alpha)), "СИЛА И ЧЕСТЬ", anchor="mm",
           font=title_font, fill=CREAM + (round(255 * title_alpha),))
    sub_alpha = smooth((t - 2.2) / .6)
    d.text((W / 2, 1410), "ХАРАКТЕР В КАЖДОЙ ДЕТАЛИ", anchor="mm",
           font=ImageFont.truetype(str(FONTS / "Oswald-Regular.ttf"), 31),
           fill=CREAM + (round(160 * sub_alpha),))
    d.line((W / 2 - 34, 1530, W / 2 + 34, 1530),
           fill=(145, 47, 36, round(230 * sub_alpha)), width=3)
    d.text((W / 2, 1612), "ВЫПУСК 001", anchor="mm",
           font=ImageFont.truetype(str(FONTS / "Oswald-Regular.ttf"), 25),
           fill=CREAM + (round(125 * sub_alpha),))
    frame = Image.alpha_composite(frame, layer).convert("RGB")
    fade = 1 - smooth((t - 5.35) / .65)
    if fade < 1:
        frame = Image.fromarray((np.asarray(frame, dtype=np.float32) * fade).astype(np.uint8))
    return frame


def render_signature(output: Path) -> None:
    mark = emblem()
    rng = np.random.default_rng(20260909)
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((x - W / 2) / 660) ** 2 + ((y - 840) / 860) ** 2))
    noise = rng.normal(0, .38, (H, W))
    data = np.stack([5 + glow * 8 + noise, 5 + glow * 7 + noise,
                     5 + glow * 5 + noise], axis=-1)
    background = Image.fromarray(np.clip(data, 0, 255).astype(np.uint8))
    signature_frame(3.6, mark, background).save(output.parent / "signature-poster.jpg", quality=95)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
               "-pixel_format", "rgb24", "-video_size", f"{W}x{H}", "-framerate", str(FPS),
               "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "17",
               "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        assert process.stdin is not None
        for i in range(6 * FPS):
            process.stdin.write(signature_frame(i / FPS, mark, background).tobytes())
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Signature encoder failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def score(output: Path) -> None:
    """Original restrained percussion, sustained fifth and metallic signature."""
    sr = 48000
    rng = np.random.default_rng(93)
    time = np.arange(DURATION * sr) / sr
    music = np.zeros_like(time)
    envelope = np.minimum(time / 3, 1) * np.clip((20.8 - time) / 2.5, 0, 1)
    for freq, gain in ((73.416, .042), (110, .019), (146.832, .012), (220, .006)):
        music += gain * np.sin(2 * np.pi * freq * time + .15 * np.sin(2 * np.pi * .17 * time)) * envelope

    def add(at: float, sound: np.ndarray, gain: float) -> None:
        start = round(at * sr)
        count = min(len(sound), len(music) - start)
        music[start:start + count] += sound[:count] * gain

    t = np.arange(round(1.7 * sr)) / sr
    phase = 2 * np.pi * (48 * t + 48 * .035 * (1 - np.exp(-t / .035)))
    hit = np.sin(phase) * np.exp(-t * 4.2)
    hit += .23 * np.sin(2 * np.pi * 109 * t) * np.exp(-t * 9)
    hit += .045 * rng.normal(0, 1, len(t)) * np.exp(-t * 65)
    hit *= np.minimum(t / .004, 1)
    for at, gain in ((.15, .34), (3.75, .25), (5, .63), (6.25, .21), (7.5, .38),
                     (8.75, .24), (10, .52), (12.5, .3), (15.15, .66), (17.1, .25)):
        add(at, hit, gain)
    t = np.arange(3 * sr) / sr
    metal = sum(np.sin(2 * np.pi * f * t) * np.exp(-t * decay) * gain
                for f, decay, gain in ((440, 2.2, .022), (907, 2.6, .014),
                                      (1433, 3.8, .009), (2371, 5.2, .004)))
    metal *= np.minimum(t / .01, 1)
    add(16.7, metal, 1)
    music *= np.clip((DURATION - time) / .65, 0, 1)
    music = np.tanh(music * 1.15)
    stereo = np.column_stack((music, music))
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes((np.clip(stereo, -.95, .95) * 32767).astype("<i2").tobytes())


def assemble(folder: Path, scratch: Path) -> None:
    inputs = [folder / "scenes" / f"{name}.mp4" for name in ("shirt-detail", "nikita-back", "nikita-front")]
    has_audio = []
    for source in inputs:
        if not source.is_file():
            raise FileNotFoundError(source)
        info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)]))
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        has_audio.append(any(s["codec_type"] == "audio" for s in info["streams"]))
        if int(stream["width"]) != W or int(stream["height"]) != H or float(stream["duration"]) < 5:
            raise ValueError(f"Unexpected source geometry or duration: {source}")
    # Encode one scene at a time to bound decoder buffers on an 8 GB Mac.
    segments = []
    for i, source in enumerate(inputs):
        target = scratch / f"segment-{i}.mp4"
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-i", str(source),
            "-t", "5", "-an", "-vf", "fps=30,setsar=1,format=yuv420p" + (",fade=t=in:d=0.35" if i == 0 else ""),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-threads", "2", str(target))
        segments.append(target)
    segments.append(scratch / "signature.mp4")
    # Names are fixed, generated locally; the manifest contains no user paths.
    concat = scratch / "segments.txt"
    concat.write_text("".join(f"file '{path.name}'\n" for path in segments))
    silent = scratch / "picture.mp4"
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
        "-i", str(concat), "-an", "-c:v", "copy", "-movflags", "+faststart", str(silent))
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1"]
    for path in [silent, *inputs, scratch / "score.wav"]:
        args += ["-threads", "2", "-i", str(path)]
    filters = []
    for i, present in enumerate(has_audio):
        source = f"[{i + 1}:a]" if present else "anullsrc=r=48000:cl=stereo,"
        filters.append(source + f"aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration=5,asetpts=PTS-STARTPTS[a{i}]")
    filters += ["[a0][a1][a2]concat=n=3:v=0:a=1,volume=0.22,afade=t=out:st=14.5:d=0.5,apad=whole_dur=21[room]",
                "[4:a][room]amix=inputs=2:normalize=0,loudnorm=I=-17:TP=-1.5:LRA=9[a]"]
    master = folder / "sila-i-chest-campaign-1080.mp4"
    args += ["-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]", "-t", str(DURATION),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(master)]
    run(*args)
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-i", str(master), "-vf", "scale=720:1280",
        "-c:v", "libx264", "-crf", "21", "-preset", "fast", "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(folder / "sila-i-chest-campaign-mobile.mp4"))
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "13.8", "-i", str(master),
        "-frames:v", "1", "-q:v", "2", str(folder / "campaign-poster.jpg"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    render_signature(args.scratch / "signature.mp4")
    score(args.scratch / "score.wav")
    if not args.prepare_only:
        assemble(args.folder, args.scratch)


if __name__ == "__main__":
    main()
