"""A 60-second highlight edit from the verified client overview recording.

The application region is not altered: only the surrounding presentation rail,
short captions, intro/outro and music are replaced. No runtime or business edits.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess

import imageio_ffmpeg
import numpy as np
from PIL import Image,ImageDraw,ImageFont

from render_client_overview import ambient_music,duration

ROOT=Path(__file__).resolve().parents[2]
W,H=1920,1080
FPS=25
INK='#edf0e7';MUTED='#a8b9ad';ACCENT='#d2dea0'

GROUPS={
 'selection':('01 / ВЫБОР','Найти.\nСравнить.\nСохранить.','Поиск по каталогу,\nразмеру и бюджету.','Только реальные данные каталога.'),
 'purchase':('02 / ПОКУПКА','Проверить.\nПодтвердить.\nОформить.','Состав и сумма видны\nдо создания покупки.','Оплата в записи — только тестовая.'),
 'team':('03 / КОМАНДА','Один заказ.\nОбщая\nистория.','Менеджер, склад\nи покупатель видят\nсвязанные события.','Права и действия разделены по ролям.'),
 'control':('04 / КОНТРОЛЬ','Поддержка.\nФинансы.\nБез путаницы.','Обращение связано\nс покупкой, а деньги —\nс финансовым журналом.','Смена рабочего статуса не меняет оплату.'),
 'return':('05 / ПОВТОР','Вернуться.\nПересчитать.\nКупить снова.','Повтор — в новую корзину.\nПромокод — по правилам.','Старый платёж не повторяется.'),
 'alerts':('06 / УВЕДОМЛЕНИЯ','Нужный\nсигнал.\nПо согласию.','Размер или цена —\nотдельная разовая\nподписка.','Не резерв. Отключение в один шаг.'),
}

# Readable outcomes and brief real interactions, without long input wizards.
# Durations sum to exactly 1500 frames / 60 seconds.
SHOTS=[
 {'key':'intro','seconds':4,'card':'intro'},
 {'key':'search','seconds':3,'start':11.9,'group':'selection','caption':'Поиск по словам, размеру, бюджету и подтверждённому наличию.'},
 {'key':'compare','seconds':4,'start':34.5,'group':'selection','caption':'Избранное и сравнение — без выдуманных характеристик.'},
 {'key':'checkout','seconds':5,'start':60.2,'group':'purchase','caption':'Сначала — согласие на данные и проверка состава. Затем — подтверждение.'},
 {'key':'paid','seconds':4,'start':77.0,'group':'purchase','caption':'Одна покупка. Отдельный резерв. Никаких реальных списаний в демо.'},
 {'key':'assembly','seconds':4.48,'start':93.3,'group':'team','caption':'Ответственный и этапы сборки — в рабочем месте менеджера.'},
 {'key':'delivery','seconds':4.52,'start':122.2,'group':'team','caption':'Стоимость и условия доставки отдельно принимает покупатель.'},
 {'key':'support','seconds':3,'start':146.5,'group':'control','caption':'Ответ поддержки остаётся в истории обращения.'},
 {'key':'finance','seconds':3,'start':183.2,'group':'control','caption':'Финансовые события не подменяются статусами сборки.'},
 {'key':'repeat','seconds':4,'start':193.0,'group':'return','caption':'Повтор заново проверяет состав, цену и наличие.'},
 {'key':'discount','seconds':5,'start':232.8,'group':'return','caption':'Промокод показывает новый итог. Старый счёт не пересчитывается.'},
 {'key':'consent','seconds':5,'start':244.8,'group':'alerts','caption':'Уведомление включается только отдельным согласием.'},
 {'key':'signal','seconds':6,'start':264.2,'group':'alerts','caption':'Один сигнал о размере — без автоматической покупки. Отписка: /alertsoff.'},
 {'key':'outro','seconds':5,'card':'outro'},
]


def font(size,bold=False):
    return ImageFont.truetype(str(ROOT/'miniapp/fonts'/('manrope-800.woff2' if bold else 'manrope-400.woff2')),size)


def text(draw,xy,value,size=24,fill=INK,bold=False,spacing=None):
    gap=spacing if spacing is not None else int(size*.1)
    for i,line in enumerate(value.split('\n')):
        draw.text((xy[0],xy[1]+i*(size+gap)),line,font=font(size,bold),fill=fill,anchor='lt')


def gradient(width,height):
    y,x=np.mgrid[:height,:width]
    glow=np.maximum(0,1-np.sqrt(((x/width-.64)/1.1)**2+((y/height-.22)/1.35)**2))
    base=np.array([13,17,18],dtype=float)
    delta=np.array([13,22,13],dtype=float)
    return Image.fromarray(np.clip(base+glow[...,None]*delta,0,255).astype('uint8')).convert('RGBA')


def brand(draw,x=68,y=111):
    draw.rectangle((x,y,x+55,y+63),outline='#748160',width=1)
    text(draw,(x+13,y+10),'V',37,ACCENT)
    text(draw,(x+74,y+8),'ВОРОЖБИТОВ',23,INK,True)
    text(draw,(x+76,y+42),'С И Л А  И  Ч Е С Т Ь',10,MUTED)


def badge(draw):
    draw.rounded_rectangle((1490,29,1863,70),radius=5,fill='#202a21',outline='#536249',width=1)
    text(draw,(1506,43),'ДЕМО · БЕЗ РЕАЛЬНЫХ СПИСАНИЙ',13,ACCENT)


def make_overlay(shot,index,elapsed,work):
    image=Image.new('RGBA',(W,H),(0,0,0,0))
    rail=gradient(554,H)
    image.alpha_composite(rail,(0,0))
    draw=ImageDraw.Draw(image)
    draw.line((550,105,550,958),fill='#28392e',width=1)
    text(draw,(68,37),'КОРОТКОЕ ДЕМО / 60 СЕКУНД',12,MUTED)
    brand(draw)
    kicker,title,deck,note=GROUPS[shot['group']]
    text(draw,(69,253),kicker,16,ACCENT,True)
    text(draw,(66,307),title,56,INK,False,spacing=7)
    text(draw,(69,526),deck,25,MUTED,False,spacing=10)
    draw.line((70,684,440,684),fill='#4a5d47',width=1)
    # Wrap the benefit copy in a calm, readable lower-rail callout.
    words=note.split();lines=[];line=''
    for word in words:
        candidate=(line+' '+word).strip()
        if draw.textlength(candidate,font=font(23))>407 and line:lines.append(line);line=word
        else:line=candidate
    if line:lines.append(line)
    text(draw,(69,719),'\n'.join(lines),23,ACCENT,False,spacing=9)
    text(draw,(70,921),'ФРАГМЕНТЫ ВЕБ-ДЕМОНСТРАЦИИ\nРЕАЛЬНЫХ ОБРАБОТЧИКОВ БОТА',11,MUTED,False,spacing=7)
    # Replace the long edition's captions and progress, never the bot message area.
    draw.rectangle((558,965,1919,1054),fill='#0d1112')
    draw.rectangle((585,984,588,1033),fill=ACCENT)
    caption=shot['caption'];lines=[];line=''
    for word in caption.split():
        proposed=(line+' '+word).strip()
        if draw.textlength(proposed,font=font(25))>1240 and line:lines.append(line);line=word
        else:line=proposed
    if line:lines.append(line)
    text(draw,(608,983),'\n'.join(lines),25,INK,False,spacing=8)
    draw.rectangle((0,1054,1919,1079),fill='#0d1112')
    draw.line((67,1061,1863,1061),fill='#344738',width=2)
    draw.line((67,1061,int(67+1796*(elapsed+shot['seconds'])/60),1061),fill=ACCENT,width=3)
    path=work/f'{index:02d}-{shot["key"]}-overlay.png';image.save(path)
    return path


def make_card(kind,work):
    if kind=='intro':
        source=Image.open(ROOT/'ui-checks/client-overview/cover.jpg').convert('RGBA')
        draw=ImageDraw.Draw(source)
        # A compact edition label in the existing neutral top margin.
        draw.rounded_rectangle((65,22,587,79),radius=6,fill='#17231c',outline='#455b42')
        text(draw,(84,40),'60 СЕКУНД / КОРОТКОЕ ДЕМО',17,ACCENT,True)
        return source
    image=gradient(W,H);draw=ImageDraw.Draw(image);brand(draw,88,121);badge(draw)
    text(draw,(91,268),'ОТ ДЕМОНСТРАЦИИ — К ЗАПУСКУ',20,ACCENT,True)
    text(draw,(84,341),'Логика работает.\nДальше — запуск\nв Telegram.',100,INK,False,spacing=14)
    text(draw,(92,816),'Показан тестовый контур.\nРеальные платежи и отправления не выполнялись.',28,MUTED,False,spacing=15)
    draw.line((88,1009,1824,1009),fill=ACCENT,width=2)
    return image


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,default=Path('/home/user/.cache/bot-video/final/capture.json'))
    parser.add_argument('--output',type=Path,default=ROOT/'ui-checks/client-overview')
    parser.add_argument('--work',type=Path,default=Path('/home/user/.cache/bot-video/short'))
    parser.add_argument('--stills',action='store_true',help='Restore the edit from verified UI checkpoints if original video was not retained')
    args=parser.parse_args();args.work.mkdir(parents=True,exist_ok=True);args.output.mkdir(parents=True,exist_ok=True)
    capture=json.loads(args.capture.read_text());assert not capture['errors']
    assert capture['timeline'][-1]['kind']=='complete'
    if not args.stills:
        assert not capture['dry'];raw=Path(capture['raw_video']);assert raw.exists()
    else:
        raw=None
        cover=ROOT/'ui-checks/client-overview/cover.jpg';cover.parent.mkdir(parents=True,exist_ok=True)
        Image.open(args.capture.parent/'00-title.png').convert('RGB').save(cover,quality=92)
    checkpoints={'search':'01-search','compare':'02-comparison','checkout':'03-checkout','paid':'04-paid',
                 'assembly':'05-assembly','delivery':'06-delivery','support':'07-support','finance':'09-receipts',
                 'repeat':'10-repeat','discount':'12-discount','consent':'13-alert-consent','signal':'14-alert-sent'}
    assert sum(round(s['seconds']*FPS) for s in SHOTS)==1500
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    jobs=[];elapsed=0
    for i,shot in enumerate(SHOTS):
        frames=round(shot['seconds']*FPS);clip=args.work/f'{i:02d}-{shot["key"]}.mp4'
        if 'card' in shot:
            png=args.work/f'{i:02d}-{shot["key"]}.png';make_card(shot['card'],args.work).convert('RGB').save(png)
            cmd=[ffmpeg,'-y','-loglevel','error','-loop','1','-framerate',str(FPS),'-i',str(png)]
            vf='format=yuv420p'
        else:
            overlay=make_overlay(shot,i,elapsed,args.work)
            if args.stills:
                frame=args.capture.parent/(checkpoints[shot['key']]+'.png');assert frame.exists()
                cmd=[ffmpeg,'-y','-loglevel','error','-loop','1','-framerate',str(FPS),'-i',str(frame),'-loop','1','-framerate',str(FPS),'-i',str(overlay)]
            else:
                cmd=[ffmpeg,'-y','-loglevel','error','-ss',str(shot['start']),'-i',str(raw),'-loop','1','-framerate',str(FPS),'-i',str(overlay)]
            cmd+=['-filter_complex','[0:v]fps=25,setpts=PTS-STARTPTS[b];[b][1:v]overlay=0:0:format=auto,format=yuv420p[v]','-map','[v]']
            vf=None
        if vf:cmd+=['-vf',vf]
        cmd+=['-an','-frames:v',str(frames),'-r',str(FPS),'-c:v','libx264','-preset','fast','-crf','20','-threads','2','-pix_fmt','yuv420p',str(clip)]
        jobs.append((cmd,clip));elapsed+=frames/FPS
    def render(job):
        cmd,clip=job;subprocess.run(cmd,check=True);print('Rendered',clip.name,flush=True);return clip
    with ThreadPoolExecutor(max_workers=2) as pool:clips=list(pool.map(render,jobs))
    manifest=args.work/'clips.txt';manifest.write_text(''.join("file '"+str(p)+"'\n" for p in clips))
    music=args.work/'ambient-60s.wav';audio=ambient_music(music,60)
    target=args.output/'vorozhbitov-bot-demo-60s.mp4'
    subprocess.run([ffmpeg,'-y','-hide_banner','-f','concat','-safe','0','-i',str(manifest),'-i',str(music),
                    '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','112k','-ar','48000','-t','60',
                    '-metadata','title=ВОРОЖБИТОВ — короткое видеодемо, 60 секунд',
                    '-metadata','comment=Edited excerpts from a verified offline browser demonstration. Synthetic data; no actual charges or shipments. Russian titles, original music, no narration.',
                    '-movflags','+faststart',str(target)],check=True)
    report={'file':str(target),'duration_seconds':duration(ffmpeg,target),'resolution':[W,H],'fps':FPS,'video_codec':'H.264','audio_codec':'AAC',
            'size_bytes':target.stat().st_size,'presentation':'60-second edited highlights; shorter explanatory rail and captions, no narration',
            'source':('Verified UI checkpoint edit restored from an executed walkthrough' if args.stills else 'Verified offline browser recording; application panels and counters unaltered'),'shots':SHOTS,**audio}
    (args.output/'short-video-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('file','duration_seconds','size_bytes')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
