#!/usr/bin/env python3
"""Re-edit the existing moving Wan scenes; keep the 30s welcome independent."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenes', type=Path, required=True)
    parser.add_argument('--signature', type=Path, required=True)
    parser.add_argument('--portrait', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    sources = {name: args.scenes / (name + '.mp4') for name in ('shirt-detail', 'nikita-back', 'nikita-front')}
    for path in [*sources.values(), args.signature, args.portrait, args.audio]:
        if not path.is_file():
            raise FileNotFoundError(path)
    output = args.output_dir / 'campaign-live-v2.mp4'
    targets = {args.output_dir / n for n in ('campaign-live-v2.mp4', 'campaign-live-v2.jpg', 'campaign-live-v2-thumb.jpg')}
    inputs = {p.resolve() for p in [*sources.values(), args.signature, args.portrait, args.audio]}
    if {p.resolve() for p in targets} & inputs:
        raise ValueError('Output must not replace an input')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.nice(10)
    grade = 'eq=contrast=1.025:brightness=-0.003:saturation=0.90'
    # Exact integer frame counts, with cuts on the existing percussion accents.
    shots = [
        (sources['shirt-detail'], .1, 114, 'scale=720:1280,' + grade + ',fade=t=in:d=0.25'),
        (sources['nikita-back'], 0, 150, 'scale=720:1280,' + grade),
        # Product close-up removes the old talking-face generation from the film.
        (sources['nikita-front'], .5, 111, 'crop=744:1320:168:600,scale=720:1280,' + grade),
        (sources['shirt-detail'], 3.7, 39, 'crop=864:1536:108:192,scale=720:1280,' + grade),
        (args.portrait, 0, 45, "scale=900:1124,crop=632:1124:134:0,scale=720:1280," + grade),
        (args.signature, .3, 171, 'scale=720:1280'),
    ]
    with tempfile.TemporaryDirectory(prefix='.live-v2-', dir=args.output_dir) as folder:
        stage = Path(folder)
        for i, (source, start, frames, filters) in enumerate(shots):
            command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1']
            if source == args.portrait:
                command += ['-loop', '1', '-framerate', '30']
            else:
                command += ['-ss', str(start)]
            command += ['-i', str(source), '-an', '-vf', filters + ',fps=30,setsar=1,format=yuv420p',
                        '-frames:v', str(frames), '-c:v', 'libx264', '-threads', '1', '-preset', 'fast', '-crf', '21',
                        '-video_track_timescale', '15360', str(stage / f'{i}.mp4')]
            run(*command)
            print(f'Shot {i + 1}/6 encoded', flush=True)
        manifest = stage / 'shots.txt'
        manifest.write_text(''.join(f"file '{i}.mp4'\n" for i in range(6)))
        film = stage / output.name
        run('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1', '-f', 'concat', '-safe', '1', '-i', manifest,
            '-i', args.audio, '-map', '0:v:0', '-map', '1:a:0', '-t', '21', '-vf', 'scale=in_range=auto:out_range=tv:out_color_matrix=bt709,format=yuv420p',
            '-c:v', 'libx264', '-threads', '1', '-preset', 'fast', '-crf', '19', '-color_range', 'tv',
            '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-c:a', 'copy', '-movflags', '+faststart', film)
        info = probe(film)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        if (video['width'], video['height'], int(video['nb_frames'])) != (720, 1280, 630):
            raise ValueError('Video geometry/frame count mismatch')
        if not 20.95 <= float(info['format']['duration']) <= 21.05 or film.stat().st_size > 8_000_000:
            raise ValueError('Unexpected duration or file size')
        audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
        if not audio or audio['codec_name'] != 'aac' or float(audio.get('duration', 0)) < 20.95:
            raise ValueError('Full AAC soundtrack missing')
        def audio_hash(path):
            return subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-t', '21', '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        if audio_hash(film) != audio_hash(args.audio):
            raise ValueError('Soundtrack payload changed')
        for name, width in [('campaign-live-v2.jpg', 720), ('campaign-live-v2-thumb.jpg', 180)]:
            run('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-ss', '14.3', '-i', film,
                '-vf', f'scale={width}:-2', '-frames:v', '1', '-q:v', '2', stage / name)
        for name in (output.name, 'campaign-live-v2.jpg', 'campaign-live-v2-thumb.jpg'):
            (stage / name).replace(args.output_dir / name)
        print(json.dumps({'duration': info['format']['duration'], 'frames': 630, 'bytes': output.stat().st_size}))


if __name__ == '__main__':
    main()
