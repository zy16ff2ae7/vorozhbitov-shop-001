# Final campaign film

`make_video_final.py` creates a 917-frame, 30 fps photographic campaign film.
It keeps the original teaser's shot durations, cut rhythm, AAC soundtrack and
animated sword finale. Campaign stills are edited/generated imagery; motion is
camera movement and editorial transitions, not live-action footage.

Input photographs are versioned in `miniapp/assets/campaign-final/` and
`miniapp/assets/drop/tee-gym-retouched-final.jpg`. Product back/detail photographs
and flatlay remain the existing corrected references. The untouched original
`miniapp/assets/video/teaser.mp4` is the soundtrack source.

Run from the repository root:

```sh
python3 tools/video/make_video_final.py --font-dir tools/video/fonts --teaser --check-inputs
python3 tools/video/make_video_final.py --font-dir tools/video/fonts --teaser
```

The renderer streams frames, uses one encoder thread and lowers its priority.
It validates frame count, dimensions, duration, file size and original audio hash
before publishing versioned files. It does not overwrite the source teaser.

The welcome background is a muted derivative of the full film, cropped to the
photographic window (720×960 at y=154), scaled to 540×720, encoded with H.264 and
faststart. Full playback retains the 720×1280 composition and original audio.

The storefront stops background playback while hidden, during modals and after
entry. Save-data and reduced-motion users get the static portrait. A visible
pause control also handles browsers that require a gesture to start video.

## Foreground film — finished steel-signature release

The current storefront foreground film is `campaign-finished-v5.mp4`: 648 frames,
21.6 seconds at 30 fps. The silent 30-second welcome video is unchanged.

The first 317 frames preserve the audited moving edit from campaign-original-v4.
A new pendant portrait follows for 108 frames, then the exact tag for 79 frames.
The requested gray metallic sword ending is restored from the existing
`outputs/video-v4/signature-v4.mp4`, retimed from 7.2 to 4.8 seconds (144 frames).
Its original reveal, light sweep and title remain together.

The soundtrack retains the original-teaser musical character for the first
16.8 seconds. The gray ending uses its existing synchronized sound, sped up with
atempo=1.5 to preserve pitch. The join has a 0.14-second fade-out and 0.10-second
fade-in. The full 21.6-second master is normalized in two passes to -16 LUFS,
-1.8 dBTP, LRA 9, then encoded to AAC 192 kbit/s / 48 kHz.
The rejected v3 electronic composition is not used.

`make_campaign_finished_v5.py` accepts `--prefix` (campaign-original-v4.mp4),
`--signature` (gray signature-v4.mp4), `--portrait`, `--tag`, `--audio` (master WAV)
and `--output-dir`. It encodes sequentially with one thread, normalizes each
segment to BT.709/TV and identical geometry/timebase, and joins video streams
without a second full-film encode. It validates 648 frames, soundtrack duration,
geometry and file size. Delivery QA additionally checks decoding, faststart,
AAC loudness and the public player.

The portrait `tee-gym-with-tag-v2.jpg` is a built-in image-generation edit of the
existing campaign portrait. It shortens the chain, removes the erroneous Y-tail
and raises the single pendant above the shirt print. The existing product image
`assets/spin/tag-sich-01.jpg` supplies the design reference. The image is edited
campaign imagery; pixel-exact preservation of the face is not asserted.
The original generated PNG, selected web JPEG and prompt notes are retained in
`outputs/store-final` outside the repository. No new moving scenes were generated.
