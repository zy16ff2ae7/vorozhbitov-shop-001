from pathlib import Path
import subprocess, os
project=Path(__file__).resolve().parent.parent;ff=os.environ.get('FFMPEG','ffmpeg');r=project/'work/medal';out=project/'rendered';out.mkdir(parents=True,exist_ok=True)
for name,w,h,px,py,sub,bell,like,cy in [('shorts',1080,1920,65,1190,370,679,855,1450),('youtube',1920,1080,366,620,747,1134,1377,945)]:
 args=[ff,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',f'color=c=black@0:s={w}x{h}:r=30:d=6,format=rgba']
 for i in range(4):args+=['-loop','1','-framerate','30','-i',str(r/f'{name}-{i}.png')]
 args+=['-loop','1','-framerate','30','-i',str(r/'cursor.png'),'-loop','1','-framerate','30','-i',str(r/'glint.png')]
 flt=[]; prev='0:v'; intervals=['lt(t,1.6)','between(t,1.6,2.85)','between(t,2.85,4.1)','gte(t,4.1)']
 for i,e in enumerate(intervals):
  label=f'p{i}'; move=f"{py}+if(lt(t,0.8),72*exp(-7*t)*cos(16*t),0)+if(gt(t,5.3),50*(t-5.3)/0.7,0)";flt.append(f"[{prev}][{i+1}:v]overlay=x={px}:y='{move}':format=auto:shortest=1:enable='{e}'[{label}]");prev=label
 span=950 if name=='shorts' else 1188
 flt.append(f"[{prev}][6:v]overlay=x='{px}+(t-0.5)*({span}-180)/0.95':y='{py}+5':format=auto:shortest=1:enable='lt(t,0)'[shine]");prev='shine'
 x=f"if(lt(t,1.45),{sub+180}-180*max(0,(t-0.65)/0.8),if(lt(t,2.65),{sub}+({bell}-{sub})*max(0,(t-1.85)/0.8),{bell}+({like}-{bell})*min(1,max(0,(t-3.10)/0.8))))"
 y=f"{cy}+if(lt(t,1.45),70*(1-max(0,(t-0.65)/0.8)),0)"
 flt.append(f"[{prev}][5:v]overlay=x='{x}':y='{y}':format=auto:shortest=1:enable='between(t,0.65,4.7)',fade=t=in:st=0:d=0.35:alpha=1,fade=t=out:st=5.3:d=0.7:alpha=1,format=yuva444p10le[v]")
 target=out/f'Vorozhbitov_{name}_Alpha_medal_v2.mov';subprocess.run(args+['-filter_complex',';'.join(flt),'-map','[v]','-t','6','-an','-c:v','prores_ks','-profile:v','4','-pix_fmt','yuva444p10le',str(target)],check=True);print(name,'alpha',flush=True)
 for suffix,bg,crf in [('Preview','0x121216','20'),('GreenScreen','0x00ff00','18')]:
  filt=f'color=c={bg}:s={w}x{h}:r=30:d=6[bg];[bg][0:v]overlay=0:0:format=auto:shortest=1,format=yuv420p[v]'
  subprocess.run([ff,'-v','error','-y','-i',str(target),'-filter_complex',filt,'-map','[v]','-t','6','-an','-c:v','libx264','-preset','fast','-crf',crf,'-movflags','+faststart',str(out/f'Vorozhbitov_{name}_{suffix}_medal_v2.mp4')],check=True)
  print(name,suffix,flush=True)
