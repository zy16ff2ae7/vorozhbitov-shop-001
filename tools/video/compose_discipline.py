#!/usr/bin/env python3
"""Deterministic original 96 BPM / D minor campaign cue, no sampled recordings.

Eight bars: restrained introduction, percussion entrance, developed motif,
brief break before the portrait, then a resolved metal/piano-like signature.
Writes a stereo premaster; deliverables require the documented mastering pass.
"""
import argparse
import json
from pathlib import Path
import wave

import numpy as np

SR = 48000
BEAT = 60 / 96
DURATION = 20


def compose(output):
    rng = np.random.default_rng(19930909)
    mix = np.zeros((SR * DURATION, 2), dtype=np.float64)
    space = np.zeros_like(mix)

    def clock(length):
        return np.arange(round(length * SR)) / SR

    def noise(length, low, high):
        n = round(length * SR)
        freq = np.fft.rfftfreq(n, 1 / SR)
        shape = (1 - np.exp(-(freq / low) ** 4)) * np.exp(-(freq / high) ** 4)
        sound = np.fft.irfft(np.fft.rfft(rng.normal(size=n)) * shape, n=n)
        return sound / max(np.std(sound), .0001)

    def add(at, sound, gain=1, pan=0, send=0):
        start = round(at * SR)
        if start < 0 or start >= len(mix):
            raise ValueError('Event outside composition')
        count = min(len(sound), len(mix) - start)
        stereo = sound[:count, None] * np.array([np.cos((pan + 1) * np.pi / 4),
                                                np.sin((pan + 1) * np.pi / 4)]) * gain
        mix[start:start + count] += stereo
        space[start:start + count] += stereo * send

    def freq(midi):
        return 440 * 2 ** ((midi - 69) / 12)

    def kick(at, gain=1):
        t = clock(.48)
        phase = 2 * np.pi * (49 * t + 95 * .021 * (1 - np.exp(-t / .021)))
        body = np.sin(phase) * np.exp(-t * 10)
        knock = noise(.48, 1300, 4100) * np.exp(-t * 180) * .075
        add(at, (body + knock) * np.minimum(t / .001, 1), .48 * gain)

    def snare(at, gain=1):
        t = clock(.32)
        snap = noise(.32, 950, 6700) * np.exp(-t * 22) * .24
        body = (np.sin(2 * np.pi * 185 * t) + .35 * np.sin(2 * np.pi * 330 * t)) * np.exp(-t * 28) * .27
        add(at, (snap + body) * np.minimum(t / .0007, 1), .66 * gain, -.03, .18)

    def hat(at, gain, opened=False, pan=.2):
        length = .23 if opened else .07
        t = clock(length)
        sound = noise(length, 5600, 11000) * np.exp(-t * (23 if opened else 85))
        add(at, sound * np.minimum(t / .0005, 1), gain, pan, .07)

    def bass(at, note, beats, gain=.19):
        t = clock(beats * BEAT)
        f = freq(note)
        # Harmonics keep the line audible on small speakers; sub remains centered.
        tone = sum(np.sin(2 * np.pi * f * h * t) * a
                   for h, a in [(1, 1), (2, .42), (3, .24), (4, .10), (6, .045)])
        env = np.minimum(t / .018, 1) * np.minimum((len(t) / SR - t) / .065, 1)
        add(at, np.tanh(tone * 1.2) * env * np.exp(-t * .5), gain)

    def key(at, note, gain=.12, pan=0):
        t = clock(2.4)
        f = freq(note)
        # Struck-string timbre with decaying upper partials, not a pure sine bell.
        tone = sum(a * np.cos(2 * np.pi * f * h * t) * np.exp(-t * decay)
                   for h, a, decay in [(1, 1, 2.5), (2.002, .3, 4), (3.008, .12, 7), (4.02, .04, 11)])
        add(at, tone * np.minimum(t / .003, 1), gain, pan, .38)

    def metal(at, gain=.09):
        t = clock(1.6)
        tone = sum(np.sin(2 * np.pi * f * t) * a * np.exp(-t * d)
                   for f, a, d in [(587.33, .5, 3), (1351, .24, 6), (2232, .15, 9), (3717, .07, 12)])
        add(at, tone * np.minimum(t / .001, 1), gain, .13, .5)

    # Warm, low-passed stereo harmonic bed: different chord voicings per two bars.
    for bar, notes in [(0, [50, 57, 65]), (2, [46, 53, 62]), (4, [48, 55, 64]), (6, [50, 57, 65])]:
        t = clock(5)
        env = np.minimum(t / .6, 1) * np.minimum((5 - t) / .9, 1)
        for j, note in enumerate(notes):
            f = freq(note)
            tone = (np.sin(2 * np.pi * f * t) + .35 * np.sin(2 * np.pi * f * 2.001 * t))
            add(bar * 2.5, tone * env, .025, (j - 1) * .55, .4)

    # Intro: a single motif, restrained pulse and a rising texture into bar two.
    key(0, 62, .13, -.1)
    key(1.25, 69, .09, .2)
    bass(.05, 38, 2.9, .10)
    metal(.04, .075)
    t = clock(.8)
    add(1.7, noise(.8, 700, 4600) * (t / .8) ** 2 * np.minimum((.8 - t) / .025, 1), .033, 0, .2)

    roots = {1: 38, 2: 34, 3: 34, 4: 36, 5: 36, 6: 38}
    for bar in range(1, 7):
        start = bar * 2.5
        # Half-time break for the product detail, then a fuller last groove.
        kicks = [0, 1.75, 2.5] if bar % 2 else [0, 1.5, 2.75, 3.5]
        if bar == 5:
            kicks = [0, 2.5]
        for beat in kicks:
            kick(start + beat * BEAT, .92 if beat else 1)
        for beat in [1, 3]:
            if bar != 5 or beat == 3:
                snare(start + beat * BEAT)
        for step in range(8):
            if bar == 5 and step < 4:
                continue
            at = start + step * BEAT / 2 + (.012 if step % 2 else 0)
            hat(at, .018 if step % 2 else .025, step == 7, .24 if step % 2 else -.2)
        if bar in [2, 4, 6]:
            snare(start + 3.75 * BEAT, .26)
        for beat, length, interval in [(0, 1.35, 0), (1.5, .65, 0), (2.5, 1.3, 7 if bar == 4 else 0)]:
            bass(start + beat * BEAT + .012, roots[bar] + interval, length)

    # Small D-minor motif, with space between phrases and a resolved final D.
    for beat, note, gain in [(4, 62, .13), (6.5, 65, .09), (8, 69, .11), (10, 65, .08),
                             (12, 62, .12), (15, 60, .07), (16, 64, .12), (18.5, 67, .08),
                             (22, 69, .10), (24, 65, .12), (26, 64, .09), (28, 62, .16)]:
        key(beat * BEAT, note, gain, -.17 if note % 2 else .17)
    metal(7.5, .055)
    metal(13.75, .06)
    kick(17.5, .95)
    bass(17.5, 38, 2.4, .17)
    key(17.5, 50, .12, -.15)
    key(17.5, 57, .075, .15)
    metal(17.5, .10)

    # Short, asymmetric room/delay taps. Dry rhythm and sub stay in the center.
    for delay, gain, swap in [(.047, .20, False), (.083, .14, True), (.157, .17, True),
                               (.313, .18, True), (.469, .10, False), (.625, .065, True)]:
        n = round(delay * SR)
        mix[n:] += space[:-n, ::-1] * gain if swap else space[:-n] * gain
    t = np.arange(len(mix)) / SR
    mix *= (np.minimum(t / .008, 1) * np.clip((DURATION - .04 - t) / 1.05, 0, 1))[:, None]
    peak = float(np.max(np.abs(mix)))
    mix *= .82 / max(peak, .001)
    if not np.isfinite(mix).all():
        raise ValueError('Non-finite mix')
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), 'wb') as handle:
        handle.setparams((2, 2, SR, 0, 'NONE', 'not compressed'))
        handle.writeframes((mix * 32767).astype('<i2').tobytes())
    print(json.dumps({'bpm': 96, 'key': 'D minor', 'seconds': DURATION,
                      'bars': 8, 'original_synthesis': True, 'premaster_peak': .82}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    compose(parser.parse_args().output)
