#!/usr/bin/env python3
"""Кампания v10: язык v9 + главы в родной типографике бренда (стиль v4).

Чего не хватало v9: голос бренда посередине фильма. Возвращаю его не
субтитрами (ошибка v8), а родной грамматикой глав оригинального фильма:
верхняя брендовая строка, нижняя глава — крупная дисплейная строка с
подстрочной заметкой и красной линией прогресса выпуска. Главы стоят
только на трёх товарных планах и дышат fade-in/out; между ними кадр чист.

Состав и звук наследуют v9: префикс 42–403 без портрета, фейд из тьмы,
сигнатура v7; звук — хвост музыки мастера с дакингом под финальные
строки, розовым райзером 0.5 с и loudnorm −16.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image
from make_video_v4 import FPS, Film, IVORY, MUTED, RED, encode_frames, smooth, span

FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())

FIRST_FRAME = 42          # портрет 0–41 исключён по указанию владельца
LAST_FRAME = 403
PREFIX_FRAMES = LAST_FRAME - FIRST_FRAME + 1
SIGNATURE_FRAMES = 186
FRAMES = PREFIX_FRAMES + SIGNATURE_FRAMES
DURATION = FRAMES / FPS
NAME = "campaign-v10"
STEEL = (150, 164, 180)
DIM = (168, 168, 162)
WIDTH, HEIGHT = 720, 1280
FRAME_BYTES = WIDTH * HEIGHT * 3

# Главы в локальном времени префикса (сек): план, заметка, титул.
CHAPTERS = (
    ((144 - FIRST_FRAME) / FPS, (192 - FIRST_FRAME) / FPS,
     "01 / ФУТБОЛКА · 4 900 ₽", "ПЕРВЫЙ ТИРАЖ"),
    ((234 - FIRST_FRAME) / FPS, (297 - FIRST_FRAME) / FPS,
     "02 / НАШИВКА У ПОДОЛА", "ФАКТУРА И ЗНАК"),
    ((350 - FIRST_FRAME) / FPS, (403 - FIRST_FRAME) / FPS,
     "03 / ЖЕТОН · 1 900 ₽", "СИЛА В МЕТАЛЛЕ"),
)


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


def render_prefix(master: Path, out_path: Path, film: Film) -> None:
    """Префикс мастера с главами: сырой конвейер + PIL-типографика."""
    graph = (
        f"[0:v]select='between(n,{FIRST_FRAME},{LAST_FRAME})',setpts=N/{FPS}/TB,"
        f"fps={FPS},fade=t=in:st=0:d=0.35,"
        f"unsharp=5:5:0.35:5:5:0.0,eq=saturation=1.07,format=rgb24[out]"
    )
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
                raise ValueError(f"prefix: коротки кадр {index}")
            frame = Image.frombytes("RGB", (WIDTH, HEIGHT), data)
            t = index / FPS
            p = index / (PREFIX_FRAMES - 1)
            top = smooth(min(1., t / .5))
            film.text(frame, "ВОРОЖБИТОВ", (32, 49), 17, tracking=3, alpha=top)
            film.text(frame, "ВЫПУСК 001", (688, 51), 13, color=MUTED,
                      tracking=2, align="right", alpha=top)
            for start, end, note, title in CHAPTERS:
                if not (start <= t < end):
                    continue
                local, dur = t - start, end - start
                a = smooth(min(1., local / .45)) * smooth(min(1., (dur - local) / .25))
                if a <= 0:
                    continue
                layer = film.shadow.copy()
                layer.putalpha(layer.getchannel("A").point(lambda v: int(v * a)))
                frame = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
                film.text(frame, note, (34, 1072), 14, color=MUTED,
                          tracking=1.6, alpha=a, max_width=652)
                film.text(frame, title, (32, 1122 + (1 - a) * 18), 65,
                          display=True, alpha=a, max_width=656)
            film.line(frame, (34, 1230, 686, 1230), MUTED, .21 * top)
            film.line(frame, (34, 1230, 34 + 652 * p, 1230), RED, .9 * top, 2)
            encode.stdin.write(frame.tobytes())
            index += 1
    finally:
        decode.stdout.close()
        encode.stdin.close()
    if decode.wait() != 0 or encode.wait() != 0:
        raise RuntimeError("prefix: ffmpeg failed")
    if index != PREFIX_FRAMES:
        raise ValueError(f"prefix: кадров {index}, ждали {PREFIX_FRAMES}")


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
    stage = Path(tempfile.mkdtemp(prefix=".campaign-v10-", dir=out))

    prefix = stage / "prefix.mp4"
    render_prefix(args.master, prefix, Film(WIDTH))

    signature = stage / "signature.mp4"
    design = FinalCard(WIDTH)
    encode_frames((design.signature(i) for i in range(SIGNATURE_FRAMES)),
                  (WIDTH, HEIGHT), SIGNATURE_FRAMES, signature, str(FFMPEG), crf="19")

    concat_list = stage / "concat.txt"
    concat_list.write_text(
        f"file '{prefix.resolve()}'\nfile '{signature.resolve()}'\n", encoding="utf-8")
    picture = stage / "picture.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", picture)

    master_dur = duration_of(args.master)
    a_off = max(0., master_dur - DURATION)
    sig_start = DURATION - SIGNATURE_FRAMES / FPS
    duck_a, duck_b = sig_start + .8, sig_start + 4.
    riser_at = max(0., sig_start - .5)
    audio = stage / "audio.m4a"
    run(FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", args.master,
        "-f", "lavfi", "-i",
        "anoisesrc=color=pink:sample_rate=48000:duration=0.5:amplitude=0.16",
        "-filter_complex",
        f"[0:a]atrim=start={a_off:.3f},asetpts=PTS-STARTPTS,"
        f"volume=0.8:enable='between(t,{duck_a:.3f},{duck_b:.3f})'[a0];"
        f"[1:a]afade=t=in:st=0:d=0.5,afade=t=out:st=0.42:d=0.08,"
        f"adelay={int(riser_at * 1000)}|{int(riser_at * 1000)}[a1];"
        f"[a0][a1]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[out]",
        "-map", "[out]", "-t", f"{DURATION:.3f}",
        "-c:a", "aac", "-b:a", "192k", audio)

    final = out / f"{NAME}.mp4"
    run(FFMPEG, "-nostdin", "-v", "error", "-i", picture, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
        "-t", f"{DURATION:.3f}", "-movflags", "+faststart", final)

    got = duration_of(final)
    if abs(got - DURATION) > .08:
        raise ValueError(f"duration drift: {got} vs {DURATION}")
    size = final.stat().st_size
    if size >= 6_000_000:
        raise ValueError(f"teaser too big: {size} bytes")
    for suffix, width, at in [(".jpg", 720, .9), ("-thumb.jpg", 180, .9),
                              ("-end.jpg", 720, DURATION - .1),
                              ("-chapter.jpg", 720, (144 - FIRST_FRAME) / FPS + .8)]:
        run(FFMPEG, "-nostdin", "-v", "error", "-ss", f"{at:.2f}", "-i", final,
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-y",
            out / f"{NAME}{suffix}")
    print(f"готово: {final} · {got:.2f} с · {size / 1e6:.2f} МБ")


if __name__ == "__main__":
    sys.exit(main())
