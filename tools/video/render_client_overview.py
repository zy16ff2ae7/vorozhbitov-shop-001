"""Package the captured UI overview as a shareable H.264/AAC MP4.

Original, quiet synthesized music only; no voice model, licensed song or live
Telegram audio. Generated material belongs in the ignored ui-checks directory.
Requires Pillow, numpy and imageio-ffmpeg (build-time tools, not runtime deps).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import wave

import imageio_ffmpeg
import numpy as np
from PIL import Image


def duration(ffmpeg, path):
    result = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path)], capture_output=True, text=True)
    match = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', result.stderr)
    if not match:
        raise ValueError('Cannot read video duration: ' + result.stderr[-500:])
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def ambient_music(path, seconds, rate=48000):
    """A soft original four-chord ambient bed, not adapted from a recording."""
    count = int((seconds + .15) * rate)
    music = np.zeros((count, 2), dtype=np.float32)
    beat = 60 / 88
    bar = beat * 8
    chords = ((45, 52, 59, 60), (41, 48, 55, 57), (48, 55, 62, 64), (43, 50, 57, 59))

    def add_note(at, midi, length, gain, pan, pluck=False):
        start = int(at * rate)
        if start >= count:
            return
        n = min(int(length * rate), count - start)
        t = np.arange(n, dtype=np.float32) / rate
        freq = 440 * 2 ** ((midi - 69) / 12)
        if pluck:
            env = (1 - np.exp(-t * 100)) * np.exp(-t * 1.8)
            tone = np.sin(2*np.pi*freq*t) + .24*np.sin(2*np.pi*freq*2*t) + .06*np.sin(2*np.pi*freq*3*t)
        else:
            env = (1 - np.exp(-t * 1.1)) * np.minimum(1, np.maximum(0, (length-t)/1.8))
            tone = .75*np.sin(2*np.pi*freq*t) + .20*np.sin(2*np.pi*freq*1.002*t) + .05*np.sin(2*np.pi*freq*2*t)
        env *= np.minimum(1, (n - np.arange(n)) / (rate * .04))
        tone = (tone * env * gain).astype(np.float32)
        music[start:start+n, 0] += tone * np.sqrt((1-pan)/2)
        music[start:start+n, 1] += tone * np.sqrt((1+pan)/2)

    for b, at in enumerate(np.arange(0, seconds, bar)):
        chord = chords[b % len(chords)]
        for i, note in enumerate(chord):
            add_note(at, note, bar + 1.0, .038 if i == 0 else .024, (-.3, .25, -.45, .5)[i])
        for j, index in enumerate((0, 2, 1, 3)):
            add_note(at + beat * (j*2+.5), chord[index] + 12, 2.6, .035, (-.25, .3, -.4, .25)[j], True)
    timeline = np.arange(count, dtype=np.float32) / rate
    fade = np.minimum(1, timeline/1.4) * np.minimum(1, np.maximum(0, (seconds-timeline)/3.2))
    music *= fade[:, None]
    rms = float(np.sqrt(np.mean(music**2)))
    if rms:
        music *= .034 / rms
    music = np.clip(music, -.22, .22)
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((music * 32767).astype('<i2').tobytes())
    return {'music': 'Original synthesized ambient bed, 88 BPM; no speech',
            'rms_dbfs': round(20*np.log10(float(np.sqrt(np.mean(music**2)))), 2),
            'peak_dbfs': round(20*np.log10(float(np.max(np.abs(music)))), 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, default=Path('/home/user/.cache/bot-video/final/capture.json'))
    parser.add_argument('--output', type=Path, default=Path('ui-checks/client-overview'))
    parser.add_argument('--speed', type=float, default=1.2)
    parser.add_argument('--trim', type=float, default=.75)
    args = parser.parse_args()
    info = json.loads(args.capture.read_text())
    if info['errors'] or info['dry']:
        raise ValueError('Only a completed, verified recording can be exported')
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    raw = Path(info['raw_video'])
    seconds = (duration(ffmpeg, raw) - args.trim) / args.speed
    args.output.mkdir(parents=True, exist_ok=True)
    work = args.capture.parent
    music_path = work / 'original-ambient.wav'
    audio_info = ambient_music(music_path, seconds)

    chapters = [{'start': 0, 'title': 'Вступление'}]
    for row in info['timeline']:
        if row['kind'] == 'chapter':
            chapters.append({'start': max(0, (row['at']-.35)/args.speed), 'title': row['kicker'].capitalize()})
        if row['kind'] == 'outro':
            chapters.append({'start': max(0, (row['at']-.35)/args.speed), 'title': 'Что нужно для боевого запуска'})
    metadata = [';FFMETADATA1', 'title=ВОРОЖБИТОВ — видеообзор Telegram-бота',
                'comment=Web simulator with real bot handlers. Synthetic data and payments. No production deployment. Russian titles; original ambient music; no narration.']
    for i, chapter in enumerate(chapters):
        end = chapters[i+1]['start'] if i+1 < len(chapters) else seconds
        metadata += ['[CHAPTER]', 'TIMEBASE=1/1000', f"START={int(chapter['start']*1000)}", f'END={int(end*1000)}', 'title='+chapter['title']]
    metadata_path = work / 'chapters.ffmeta'
    metadata_path.write_text('\n'.join(metadata)+'\n')
    target = args.output / 'vorozhbitov-bot-overview.mp4'
    cmd = [ffmpeg, '-y', '-hide_banner', '-i', str(raw), '-i', str(music_path), '-f', 'ffmetadata', '-i', str(metadata_path),
           '-filter_complex', f'[0:v]trim=start={args.trim},setpts=(PTS-STARTPTS)/{args.speed},format=yuv420p[v]',
           '-map', '[v]', '-map', '1:a:0', '-map_metadata', '2', '-map_chapters', '2',
           '-r', '25', '-c:v', 'libx264', '-preset', 'medium', '-crf', '21', '-threads', '3',
           '-c:a', 'aac', '-b:a', '112k', '-ar', '48000', '-movflags', '+faststart', '-t', f'{seconds:.3f}', str(target)]
    subprocess.run(cmd, check=True)
    Image.open(work / '00-title.png').convert('RGB').save(args.output / 'cover.jpg', quality=92)
    report = {'file': str(target), 'duration_seconds': duration(ffmpeg, target), 'width': 1920, 'height': 1080,
              'video_codec': 'H.264', 'audio_codec': 'AAC', 'fps': 25, 'size_bytes': target.stat().st_size,
              'language': 'ru', 'presentation': 'Russian explanatory titles; live browser capture at 1.2x navigation speed',
              'no_narration': True, 'data': 'Synthetic offline demo, no actual charges or shipments',
              'capture_checks': info['timeline'][-1], 'browser_errors': info['errors'], 'chapters': chapters, **audio_info}
    (args.output / 'video-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
