# Тизер v2 «плёнка» — как пересобрать

`miniapp/assets/video/teaser.mp4` собран скриптом `make_video_v2_film.py`
(взят из `zy16ff2ae7/vorozhbitov-clothing-bot-sila-i-chest`, путь
`sila-i-chest/video/make_video_v2_film.py`) — это генератор, а не монтаж в редакторе:
каждый кадр рисуется в PIL и пишется в ffmpeg через пайп.

## Что делает
1080×1920, 30 fps, 30.6 с, 13 сегментов:
интро «ВОРОЖБИТОВ ПРЕДСТАВЛЯЕТ» → 8 сцен с титрами → срыв кадра в проекторе →
стробо-нарезка ч/б → длинный герой-кадр → флэтлей «ДВА ПРИНТА» → финал с мечом и ВВ.

Плёночный грейд: bleach bypass, тёплые света, холодные тени, зерно, дрожание кадра,
мерцание экспозиции, царапины, пыль, засветки. Звук синтезируется в скрипте:
барабан, хор-пад, ветер, стрёкот проектора, наковальня, «шинг» меча.

Титры набраны уставом — тем же строем букв, что и принт на груди
(`ruslan.woff2` из доноpа, сконвертирован в TTF через fontTools).

## Зависимости
`numpy`, `pillow`, `scipy`, `imageio-ffmpeg`.

## Запуск
Скрипт ждёт рядом с собой (на уровень выше `video/`) 10 фото
`sila-i-chest-NN-*.jpg` и шрифты в `video/fonts/`:
`RuslanDisplay.ttf`, `Oswald-Regular.ttf`.

```
python3 video/make_video_v2_film.py     # ~4 мин, отдаёт 1080×1920, ~32 МБ
```

Витринная версия — 720×1280, чтобы не жечь мобильный трафик:

```
ffmpeg -y -i sila-i-chest-teaser-v2-film.mp4 -vf "scale=720:1280:flags=lanczos" \
  -c:v libx264 -preset slow -crf 26 -pix_fmt yuv420p -profile:v high -level 4.0 \
  -c:a aac -b:a 96k -ac 2 -movflags +faststart miniapp/assets/video/teaser.mp4
```

Постер: `ffmpeg -ss 22.2 -i teaser.mp4 -frames:v 1 -vf scale=540:-2 teaser-poster.jpg`

## Правка сценария
Список `SHOTS` в начале файла: `(файл, титр, секунды, фокус, движение)`.
Титры приведены к тем кадрам, что реально есть в проекте, — если меняете
фото, поменяйте и титр, иначе над крышей окажется «ВЕРНОСТЬ».
