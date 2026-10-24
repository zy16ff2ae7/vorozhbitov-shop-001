#!/usr/bin/env python3
"""Кампания v9: чистый язык v7 без портрета в открытии.

Урок v8: навесная графика (титры поверх кадров, бегущая строка, CTA-плашка,
диссольвы) удешевила картинку. В v9 графика убрана полностью — остаются
камера, деликатный грейд и типографский финал v7. Единственное изменение
содержания: лицевой портрет (кадры 0–41 мастера) исключён из открытия,
фильм начинается с плана фигуры и уходит в чёрное мягким фейдом из тьмы.

Состав: 362 кадра префикса (мастер 42–403) + 186 кадров сигнатуры v7.
Звук: хвост музыки мастера, чтобы финал попадал в разрешение темы.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
import numpy as np  # noqa: F401  (Film работает с numpy внутри)
from make_video_v4 import FPS, Film, IVORY, encode_frames, span

FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())

FIRST_FRAME = 42          # портрет 0–41 исключён по указанию владельца
LAST_FRAME = 403
PREFIX_FRAMES = LAST_FRAME - FIRST_FRAME + 1
SIGNATURE_FRAMES = 186
FRAMES = PREFIX_FRAMES + SIGNATURE_FRAMES
DURATION = FRAMES / FPS
NAME = "campaign-v9"
STEEL = (150, 164, 180)
DIM = (168, 168, 162)


def run(*args: str) -> None:
    subprocess.run([str(a) for a in args], check=True)


def duration_of(path: Path) -> float:
    info = subprocess.run([str(FFMPEG), "-i", str(path)], capture_output=True, text=True).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info)
    if not match:
        raise ValueError(f"cannot probe {path}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


class FinalCard(Film):
    """Финал v7 без изменений: бренд, дроп, действие."""

    def signature(self, local_frame: int):
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
        return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True,
                        help="мастер v6 (surgery/orig-v6.mp4)")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.master.is_file():
        raise FileNotFoundError(args.master)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".campaign-v9-", dir=out))

    prefix = stage / "prefix.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-threads", "1", "-i", args.master,
        "-filter_complex",
        f"[0:v]select='between(n,{FIRST_FRAME},{LAST_FRAME})',setpts=N/{FPS}/TB,"
        f"fade=t=in:st=0:d=0.35,"
        f"unsharp=5:5:0.35:5:5:0.0,eq=saturation=1.07,format=yuv420p[out]",
        "-map", "[out]", "-an", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", prefix)

    signature = stage / "signature.mp4"
    design = FinalCard(720)
    encode_frames((design.signature(i) for i in range(SIGNATURE_FRAMES)),
                  (720, 1280), SIGNATURE_FRAMES, signature, str(FFMPEG), crf="19")

    concat_list = stage / "concat.txt"
    concat_list.write_text(
        f"file '{prefix.resolve()}'\nfile '{signature.resolve()}'\n", encoding="utf-8")
    picture = stage / "picture.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", picture)

    master_dur = duration_of(args.master)
    a_off = max(0., master_dur - DURATION)
    final = out / f"{NAME}.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-i", picture, "-i", args.master,
        "-filter_complex",
        f"[1:a]atrim=start={a_off:.3f},asetpts=PTS-STARTPTS[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{DURATION:.3f}", "-movflags", "+faststart", final)

    got = duration_of(final)
    if abs(got - DURATION) > .08:
        raise ValueError(f"duration drift: {got} vs {DURATION}")
    size = final.stat().st_size
    if size >= 6_000_000:
        raise ValueError(f"teaser too big: {size} bytes")
    for suffix, width, at in [(".jpg", 720, .9), ("-thumb.jpg", 180, .9),
                              ("-end.jpg", 720, DURATION - .1)]:
        run(FFMPEG, "-nostdin", "-v", "error", "-ss", f"{at:.2f}", "-i", final,
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-y",
            out / f"{NAME}{suffix}")
    print(f"готово: {final} · {got:.2f} с · {size / 1e6:.2f} МБ")


if __name__ == "__main__":
    sys.exit(main())
