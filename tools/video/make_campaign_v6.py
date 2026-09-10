#!/usr/bin/env python3
"""A continuous-score product film with natural-speed action and steel finale.

Uses the existing campaign footage and original score. Photographs are openly
treated as short inserts; no optical-flow motion or face animation is generated.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

from make_video_v4 import Film, FPS, encode_frames, make_audio, span
from make_campaign_finished_v5 import probe, run

PREFIX_FRAMES = 403
SIGNATURE_FRAMES = 186
FRAMES = PREFIX_FRAMES + SIGNATURE_FRAMES
DURATION = FRAMES / FPS
NAME = 'campaign-v6'


class CleanSignature(Film):
    def signature(self, local_frame):
        t = local_frame / FPS
        frame = self.background.copy()
        self.particles(frame, t, .10)
        frame = self.emblem(frame, t)
        alpha = span(t, 2.75, 3.40)
        self.text(frame, 'СИЛА И ЧЕСТЬ', (360, 905 + 16 * (1-alpha)),
                  72, display=True, align='center', max_width=640, alpha=alpha)
        # Hold the actual brand frame on completion, rather than encoded black.
        return frame


def make_master(original, scenes, stage):
    native = stage / 'continuous-original.wav'
    make_audio(original, native, 'ffmpeg')
    premaster = stage / 'premaster.wav'
    # Original music and the steel ending already share one timeline. Trim that
    # timeline once; never splice independently normalized music sections.
    graph = (
        f'[0:a]aresample=48000,atrim=start={317/FPS}:end=30.2,asetpts=PTS-STARTPTS,'
        f'afade=t=in:d=0.08,afade=t=out:st={DURATION-.85}:d=0.85[music];'
        '[1:a]aresample=48000,atrim=start=0.7:end=4.1,asetpts=PTS-STARTPTS,'
        'highpass=f=100,lowpass=f=4500,volume=2,afade=t=in:d=0.08,'
        'afade=t=out:st=3.25:d=0.15,adelay=1400:all=1[room];'
        f'[music][room]amix=inputs=2:duration=first:normalize=0,apad,'
        f'atrim=duration={DURATION}[mix]'
    )
    run('ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
        '-filter_complex_threads', '1', '-i', native, '-i', scenes / 'nikita-back.mp4',
        '-filter_complex', graph, '-map', '[mix]', '-ar', '48000', '-ac', '2', premaster)
    first = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-i', str(premaster),
                            '-af', 'loudnorm=I=-16:TP=-1.8:LRA=9:print_format=json',
                            '-f', 'null', '-'], capture_output=True, text=True, check=True)
    report_start = first.stderr.rfind('{')
    if report_start < 0:
        raise ValueError('FFmpeg did not return a loudness report')
    measured, _ = json.JSONDecoder().raw_decode(first.stderr[report_start:])
    normalizer = ('loudnorm=I=-16:TP=-1.8:LRA=9:linear=true:'
                  f'measured_I={measured["input_i"]}:measured_TP={measured["input_tp"]}:'
                  f'measured_LRA={measured["input_lra"]}:measured_thresh={measured["input_thresh"]}:'
                  f'offset={measured["target_offset"]},aresample=48000,asetpts=N/SR/TB,'
                  f'apad=whole_len={FRAMES*1600},atrim=end_sample={FRAMES*1600}')
    master = stage / 'master.wav'
    run('ffmpeg', '-nostdin', '-v', 'error', '-i', premaster, '-af', normalizer,
        '-ar', '48000', '-ac', '2', master)
    if abs(float(probe(master)['format']['duration'])-DURATION) > .001:
        raise ValueError('Master audio has an incorrect sample count')
    return master


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenes', type=Path, required=True)
    parser.add_argument('--portrait', type=Path, required=True)
    parser.add_argument('--tag', type=Path, required=True)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    sources = {n: args.scenes / f'{n}.mp4' for n in ('nikita-back', 'nikita-front', 'shirt-detail')}
    inputs = [*sources.values(), args.portrait, args.tag, args.original]
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    names = [f'{NAME}{suffix}' for suffix in ('.mp4', '.jpg', '-thumb.jpg', '-end.jpg', '-audio.wav', '-timeline.json')]
    if {p.resolve() for p in inputs} & {(args.output_dir / n).resolve() for n in names}:
        raise ValueError('Output must not replace an input')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.nice(10)
    grade = 'eq=contrast=1.02:brightness=0.002:saturation=0.82'
    portrait = "scale=900:1124,crop=632:1124:134:0,zoompan=z='1+on*0.0003':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30," + grade
    stage = Path(tempfile.mkdtemp(prefix='.campaign-v6-', dir=args.output_dir))
    try:
        master = make_master(args.original, args.scenes, stage)
        signature = stage / 'signature.mp4'
        design = CleanSignature(720)
        encode_frames((design.signature(i) for i in range(SIGNATURE_FRAMES)),
                      (720,1280), SIGNATURE_FRAMES, signature, 'ffmpeg')
        shots = [
            ('Никита', args.portrait, 0, 42, portrait),
            ('Подтягивание', sources['nikita-back'], .7, 102, 'scale=720:1280,' + grade),
            ('Принт', sources['nikita-front'], .1, 48, 'crop=744:1320:168:600,scale=720:1280,' + grade),
            ('Клинок', sources['shirt-detail'], 2.2, 42, 'crop=864:1536:108:192,scale=720:1280,' + grade),
            ('Нижняя этикетка', sources['nikita-front'], 2.4, 63, 'crop=432:768:600:1080,scale=720:1280,' + grade),
            ('Фактура', sources['shirt-detail'], 130/30, 20, 'crop=756:1344:162:288,scale=720:1280,' + grade),
            ('Жетон в образе', args.portrait, 0, 33,
             "scale=1122:1402,crop=430:764:332:327,zoompan=z='1.0+on*0.00035':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30," + grade),
            ('Жетон крупно', args.tag, 0, 53,
             "crop=432:768:488:0,zoompan=z='1+on*0.00035':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30,eq=contrast=1.0:saturation=0.55"),
            ('Серый меч', signature, 0, SIGNATURE_FRAMES, 'scale=720:1280'),
        ]
        assert sum(s[3] for s in shots) == FRAMES
        timeline = []
        position = 0
        for i, (label, source, start, frames, filters) in enumerate(shots):
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1']
            command += ['-loop', '1', '-framerate', '30'] if source in (args.portrait, args.tag) else ['-ss', str(start)]
            command += ['-i', str(source), '-an', '-vf', filters + ',fps=30,setsar=1,scale=in_range=auto:out_range=tv:out_color_matrix=bt709,format=yuv420p',
                        '-frames:v', str(frames), '-c:v', 'libx264', '-threads', '1', '-preset', 'fast', '-crf', '18',
                        '-color_range', 'tv', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-video_track_timescale', '15360', str(stage / f'{i}.mp4')]
            run(*command)
            timeline.append({'label': label, 'start': position/FPS, 'end': (position+frames)/FPS,
                             'source': str(source) if source != signature else 'CleanSignature', 'source_start': start, 'speed': 1})
            position += frames
            print(f'Shot {i+1}/{len(shots)} ready', flush=True)
        manifest = stage / 'shots.txt'
        manifest.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(shots))))
        film = stage / f'{NAME}.mp4'
        run('ffmpeg', '-nostdin', '-v', 'error', '-f', 'concat', '-safe', '1', '-i', manifest,
            '-i', master, '-map', '0:v:0', '-map', '1:a:0', '-t', DURATION,
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', film)
        info = probe(film)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
        if (video['width'], video['height'], int(video['nb_frames'])) != (720,1280,FRAMES):
            raise ValueError('Unexpected video geometry or frame count')
        if abs(float(info['format']['duration'])-DURATION) > .05 or float(audio.get('duration',0)) < DURATION-.05:
            raise ValueError(f'Incomplete picture or audio: expected={DURATION}, format={info["format"]["duration"]}, video={video.get("duration")}, audio={audio.get("duration")}')
        if film.stat().st_size > 8_000_000:
            raise ValueError('Mobile file exceeds budget')
        run('ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i', film, '-f', 'null', '-')
        for suffix, width, at in [('.jpg',720,.5),('-thumb.jpg',180,.5),('-end.jpg',720,DURATION-.1)]:
            run('ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-ss', at, '-i', film,
                '-vf', f'scale={width}:-2', '-frames:v', '1', '-q:v', '2', stage / f'{NAME}{suffix}')
        master.replace(stage / f'{NAME}-audio.wav')
        (stage / f'{NAME}-timeline.json').write_text(json.dumps(timeline,ensure_ascii=False,indent=2))
        for name in names:
            (stage / name).replace(args.output_dir / name)
        print(json.dumps({'duration': DURATION, 'frames': FRAMES, 'decode':'passed'}))
    except Exception:
        print(f'Diagnostic stage retained: {stage}', flush=True)
        raise
    else:
        shutil.rmtree(stage)


if __name__ == '__main__':
    main()
