"""Read-only watch/download page for the requested client video.

No directory listing, bot state, credentials or production service is exposed.
Run with the process tool: python3 tools/video/serve_client_video.py --port 4180
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]
VIDEO=ROOT/'deliverables/vorozhbitov-bot-demo-60s.mp4'

PAGE='''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Видеообзор — смотреть и скачать</title>
<style>
:root{color-scheme:dark;font-family:Arial,Helvetica,sans-serif;color:#eef1e8;background:#111715}*{box-sizing:border-box}body{margin:0;padding:30px 20px 40px;background:radial-gradient(ellipse at top,#27372b,transparent 65%);min-height:100vh}main{max-width:1060px;margin:auto}header{display:flex;gap:14px;align-items:center;margin-bottom:26px}.mark{border:1px solid #84936d;color:#d2dea0;font-size:30px;width:47px;height:52px;display:grid;place-items:center}.brand{letter-spacing:2px;font-size:18px;font-weight:bold}.sub{font-size:11px;color:#a8b69f;letter-spacing:2px;margin-top:6px}h1{font-size:clamp(26px,5vw,43px);letter-spacing:-1px;margin:0 0 12px}p{color:#b7c1b0;line-height:1.6}video{display:block;width:100%;aspect-ratio:16/9;background:#080b09;border:1px solid #526247;border-radius:12px;box-shadow:0 18px 65px #0005}.details{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 24px}.details span{padding:7px 12px;border:1px solid #3c4b36;border-radius:30px;font-size:12px;color:#c6d4b7}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}button,a.button{display:inline-flex;align-items:center;justify-content:center;min-height:50px;border-radius:7px;border:1px solid #aabb80;padding:13px 24px;font-size:16px;cursor:pointer;text-decoration:none;font-weight:600}a.button{background:#d2dea0;color:#172012}button{background:#253423;color:#e0eabd}#status{min-height:24px;margin:8px 0;color:#bdcaa8;font-size:14px}.note{border-left:2px solid #63774d;padding:2px 0 2px 15px;font-size:13px}footer{font-size:12px;color:#798774;margin-top:24px}button:focus-visible,a:focus-visible{outline:3px solid #e8f1c9;outline-offset:4px}@media(max-width:600px){body{padding:20px 13px}.actions>*{width:100%}}
</style></head><body><main>
<header><div class="mark">V</div><div><div class="brand">ВОРОЖБИТОВ</div><div class="sub">ВИДЕОДЕМО МАГАЗИНА</div></div></header>
<h1>Видеообзор — 60 секунд</h1>
<p>Готовый файл можно посмотреть здесь или скачать.</p>
<div class="details"><span>60 секунд</span><span>Full HD · 1920 × 1080</span><span>MP4 · около 2,5 МБ</span><span>Русские титры и музыка</span></div>
<div class="actions"><button type="button" id="play">▶ Воспроизвести</button><a class="button" href="/download" download="vorozhbitov-bot-demo-60s.mp4">↓ Скачать MP4</a></div>
<video id="player" controls playsinline preload="metadata" poster="/poster.svg"><source src="/video.mp4" type="video/mp4">Скачай файл по кнопке выше.</video>
<div id="status" role="status" aria-live="polite">Загружается информация о видео…</div>
<p class="note">Чтобы показать заказчику, скачай MP4 и отправь его как файл в Telegram. Доступ к Arena для просмотра скачанного файла не нужен. Ссылка на этот предпросмотр может требовать доступ к рабочему окружению.</p>
<footer>В ролике показано веб-демо реальных обработчиков бота. Данные, платежи и отправления — тестовые.</footer>
</main><script src="/player.js"></script></body></html>'''

SCRIPT='''const player=document.getElementById('player'),status=document.getElementById('status');
player.addEventListener('loadedmetadata',()=>{status.textContent='Видео готово: '+Math.round(player.duration)+' секунд. Нажми «Воспроизвести».';});
player.addEventListener('playing',()=>{status.textContent='Идёт воспроизведение. Файл также можно скачать кнопкой ниже.';});
player.addEventListener('error',()=>{status.textContent='Встроенный плеер не загрузился. Попробуй кнопку «Скачать MP4».';});
document.getElementById('play').addEventListener('click',async()=>{try{await player.play();}catch(_){status.textContent='Нажми ▶ непосредственно в плеере или скачай файл.';}});
'''
POSTER='''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080"><defs><linearGradient id="b" x2="1" y2="1"><stop stop-color="#273c2d"/><stop offset="1" stop-color="#0b1110"/></linearGradient></defs><rect width="1920" height="1080" fill="url(#b)"/><rect x="100" y="108" width="72" height="86" fill="none" stroke="#b6ca91"/><text x="118" y="172" fill="#d2dea0" font-family="Arial" font-size="56">V</text><text x="203" y="161" fill="#edf0e7" font-family="Arial" font-size="37" letter-spacing="4">ВОРОЖБИТОВ</text><text x="103" y="347" fill="#b4c79d" font-family="Arial" font-size="27" letter-spacing="5">КОРОТКОЕ ВИДЕОДЕМО</text><text x="95" y="489" fill="#edf0e7" font-family="Arial" font-size="100">Магазин в одном</text><text x="95" y="612" fill="#edf0e7" font-family="Arial" font-size="100">диалоге.</text><text x="105" y="798" fill="#c0cdb7" font-family="Arial" font-size="32">60 секунд · Full HD · Тестовые данные</text><circle cx="1550" cy="590" r="117" fill="#d2dea0"/><path d="M1522 528 L1522 652 L1614 590 Z" fill="#1a281b"/></svg>'''


class Handler(BaseHTTPRequestHandler):
    server_version='ClientVideo/1.0'

    def send_bytes(self,body,mime,status=200,head=False):
        if isinstance(body,str):body=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        if not head:self.wfile.write(body)

    def stream_video(self,download,head=False):
        size=VIDEO.stat().st_size;start=0;end=size-1;partial=False
        requested=self.headers.get('Range')
        if requested:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested.strip())
            if not match or not any(match.groups()):return self.range_error(size)
            left,right=match.groups()
            if left:
                start=int(left);end=min(int(right),end) if right else end
            else:
                length=int(right)
                if length<1:return self.range_error(size)
                start=max(0,size-length)
            if start>=size or end<start:return self.range_error(size)
            partial=True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type','video/mp4')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','private, max-age=0, must-revalidate')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Disposition',('attachment' if download else 'inline')+'; filename="vorozhbitov-bot-demo-60s.mp4"')
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head:return
        try:
            with VIDEO.open('rb') as source:
                source.seek(start);remaining=end-start+1
                while remaining:
                    chunk=source.read(min(65536,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):pass

    def range_error(self,size):
        self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers()

    def dispatch(self,head=False):
        route=urlsplit(self.path).path
        if route in ('/','/index.html'):return self.send_bytes(PAGE,'text/html; charset=utf-8',head=head)
        if route=='/player.js':return self.send_bytes(SCRIPT,'text/javascript; charset=utf-8',head=head)
        if route=='/poster.svg':return self.send_bytes(POSTER,'image/svg+xml; charset=utf-8',head=head)
        if route in ('/video.mp4','/download'):return self.stream_video(route=='/download',head=head)
        return self.send_bytes('Not found','text/plain; charset=utf-8',404,head)

    def do_GET(self):self.dispatch()
    def do_HEAD(self):self.dispatch(head=True)
    def log_message(self,*args):pass


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=4180);args=parser.parse_args()
    if not VIDEO.is_file():raise SystemExit('The video deliverable is missing')
    server=ThreadingHTTPServer(('0.0.0.0',args.port),Handler)
    print(f'Video watch/download page: http://0.0.0.0:{args.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
