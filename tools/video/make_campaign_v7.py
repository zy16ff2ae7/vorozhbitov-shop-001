#!/usr/bin/env python3
"""Кампания v7: хирургия мастера v6 без исходных сцен.

Что улучшено относительно v6:
1. Префикс получает деликатный детальный грейд (unsharp + возврат
   насыщенности): фактура и принт читаются на телефоне, без мыла.
2. Финальная сигнатура перерисована: брендовая строка крупнее и выше,
   под ней появилась строка дропа и призыв «смотри в боте» — фильм
   заканчивается действием для покупателя, а не пустым чёрным полем.
3. Тайминги раскрытия текста разведены, чтобы титр не мигал и успевал
   прочитаться до склейки; безопасные поля соблюдены (текст не шире
   620/720, вертикаль внутри центральной зоны).

Длительность и звук наследуются мастера v6 покадрово: 403 кадра префикса
+ 186 кадров сигнатуры, аудио дорожка копируется без изменений.
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

PREFIX_FRAMES = 403
SIGNATURE_FRAMES = 186
FRAMES = PREFIX_FRAMES + SIGNATURE_FRAMES
DURATION = FRAMES / FPS
NAME = "campaign-v7"
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
    """Финал с призывом: бренд, дроп, действие — за шесть секунд."""

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
    stage = Path(tempfile.mkdtemp(prefix=".campaign-v7-", dir=out))

    prefix = stage / "prefix.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-threads", "1", "-i", args.master,
        "-frames:v", str(PREFIX_FRAMES), "-an",
        "-vf", "unsharp=5:5:0.35:5:5:0.0,eq=saturation=1.07",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p", "-r", str(FPS), prefix)

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

    final = out / f"{NAME}.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-i", picture, "-i", args.master,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
        "-t", f"{DURATION:.3f}", "-movflags", "+faststart", final)

    got = duration_of(final)
    if abs(got - DURATION) > .08:
        raise ValueError(f"duration drift: {got} vs {DURATION}")
    for suffix, width, at in [(".jpg", 720, .5), ("-thumb.jpg", 180, .5),
                              ("-end.jpg", 720, DURATION - .1)]:
        run(FFMPEG, "-nostdin", "-v", "error", "-ss", f"{at:.2f}", "-i", final,
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-y",
            out / f"{NAME}{suffix}")
    print(f"готово: {final} · {got:.2f} с")


if __name__ == "__main__":
    sys.exit(main())
