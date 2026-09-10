#!/usr/bin/env python3
"""20-second product edit using the original teaser score and original sword ending."""
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
    parser.add_argument('--tag', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    sources = {name: args.scenes / (name + '.mp4') for name in ('shirt-detail', 'nikita-back', 'nikita-front')}
    for path in [*sources.values(), args.signature, args.portrait, args.audio, args.tag]:
        if not path.is_file():
            raise FileNotFoundError(path)
    output = args.output_dir / 'campaign-original-v4.mp4'
    targets = {args.output_dir / n for n in ('campaign-original-v4.mp4', 'campaign-original-v4.jpg', 'campaign-original-v4-thumb.jpg')}
    inputs = {p.resolve() for p in [*sources.values(), args.signature, args.portrait, args.audio, args.tag]}
    if {p.resolve() for p in targets} & inputs:
        raise ValueError('Output must not replace an input')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.nice(10)
    grade = 'eq=contrast=1.025:brightness=-0.003:saturation=0.90'
    # Source music starts at frame317; original ending begins at821.
    # Cuts follow the original soundtrack accents, including its exact 3.2s ending.
    portrait_filter = "scale=900:1124,crop=632:1124:134:0,zoompan=z='1+on*0.00025':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30,"
    shots = [
        (sources['shirt-detail'], 0, 24, 'scale=720:1280,' + grade + ',fade=t=in:d=0.12'),
        (sources['nikita-front'], .1, 66, 'crop=744:1320:168:600,scale=720:1280,' + grade),
        # One uninterrupted upper/lower/upper cycle, without speed changes/replay.
        (sources['nikita-back'], .7, 102, 'scale=720:1280,' + grade),
        (sources['shirt-detail'], 2.2, 42, 'crop=864:1536:108:192,scale=720:1280,' + grade),
        (sources['nikita-front'], 2.4, 63, 'crop=432:768:600:1080,scale=720:1280,' + grade),
        (sources['shirt-detail'], 130 / 30, 20, 'crop=756:1344:162:288,scale=720:1280,' + grade),
        (args.portrait, 0, 108, portrait_filter + grade),
        (args.tag, 0, 79, "crop=432:768:488:0,zoompan=z='1+on*0.00022':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30," + grade),
        (args.signature, 821 / 30, 96, 'scale=720:1280'),
    ]
    with tempfile.TemporaryDirectory(prefix='.original-v4-', dir=args.output_dir) as folder:
        stage = Path(folder)
        for i, (source, start, frames, filters) in enumerate(shots):
            command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1']
            if source in (args.portrait, args.tag):
                command += ['-loop', '1', '-framerate', '30']
            else:
                command += ['-ss', str(start)]
            command += ['-i', str(source), '-an', '-vf', filters + ',fps=30,setsar=1,format=yuv420p',
                        '-frames:v', str(frames), '-c:v', 'libx264', '-threads', '1', '-preset', 'fast', '-crf', '21',
                        '-video_track_timescale', '15360', str(stage / f'{i}.mp4')]
            run(*command)
            print(f'Shot {i + 1}/{len(shots)} encoded', flush=True)
        manifest = stage / 'shots.txt'
        manifest.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(shots))))
        film = stage / output.name
        run('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1', '-f', 'concat', '-safe', '1', '-i', manifest,
            '-i', args.audio, '-map', '0:v:0', '-map', '1:a:0', '-t', '20', '-vf', 'scale=in_range=auto:out_range=tv:out_color_matrix=bt709,format=yuv420p',
            '-c:v', 'libx264', '-threads', '1', '-preset', 'fast', '-crf', '19', '-color_range', 'tv',
            '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', film)
        info = probe(film)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        if (video['width'], video['height'], int(video['nb_frames'])) != (720, 1280, 600):
            raise ValueError('Video geometry/frame count mismatch')
        if not 19.95 <= float(info['format']['duration']) <= 20.05 or film.stat().st_size > 8_000_000:
            raise ValueError('Unexpected duration or file size')
        audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
        if not audio or audio['codec_name'] != 'aac' or float(audio.get('duration', 0)) < 19.95:
            raise ValueError('Full AAC soundtrack missing')
        for name, width in [('campaign-original-v4.jpg', 720), ('campaign-original-v4-thumb.jpg', 180)]:
            run('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-ss', '12.0', '-i', film,
                '-vf', f'scale={width}:-2', '-frames:v', '1', '-q:v', '2', stage / name)
        for name in (output.name, 'campaign-original-v4.jpg', 'campaign-original-v4-thumb.jpg'):
            (stage / name).replace(args.output_dir / name)
        print(json.dumps({'duration': info['format']['duration'], 'frames': 600, 'bytes': output.stat().st_size}))


if __name__ == '__main__':
    main()
