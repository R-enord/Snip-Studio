#!/usr/bin/env python3
"""SnipRiot vertical clip maker (text-only style).
Usage:
  python3 make_clip.py IN.mp4 START END "HOOK TEXT" "@credit" OUT.mp4 [--layout fit|crop|cam] [--cam x,y,w,h] [--srt subs.srt] [--focus 0.5]
  START/END: seconds or HH:MM:SS
  fit  = full 16:9 frame centered on a blurred fill (default, safest)
  crop = 9:16 center crop of gameplay (--focus 0..1 shifts crop left/right)
  cam  = facecam box (--cam x,y,w,h in source pixels) on top, gameplay crop below
"""
import argparse, subprocess, textwrap, tempfile, os, json, re
FONT = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
def ts(v):
    if ":" in v:
        p=[float(x) for x in v.split(":")]; s=0
        for x in p: s=s*60+x
        return s
    return float(v)
def probe(f):
    o=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","json",f],capture_output=True,text=True).stdout
    s=json.loads(o)["streams"][0]; return s["width"],s["height"]
ap=argparse.ArgumentParser()
for a in ["inp","start","end","hook","credit","out"]: ap.add_argument(a)
ap.add_argument("--layout",default="fit"); ap.add_argument("--cam"); ap.add_argument("--srt"); ap.add_argument("--focus",type=float,default=0.5)
a=ap.parse_args()
st,en=ts(a.start),ts(a.end); dur=en-st
tmp=tempfile.mkdtemp()
clean=re.sub(r"[^\w\s'!?.,:%$#&@-]","",a.hook.upper()).strip()  # emoji can't render in burned text; keep emoji in captions only
lines=textwrap.wrap(clean,width=18)[:3]
hfs=[]
for i,l in enumerate(lines):
    p=os.path.join(tmp,f"hook{i}.txt"); open(p,"w").write(l); hfs.append(p)
cf=os.path.join(tmp,"credit.txt"); open(cf,"w").write(a.credit)
W,H=1080,1920
if a.layout=="fit":
    v=("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.15[bg];"
       "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2+60[v0]")
elif a.layout=="crop":
    sw,sh=probe(a.inp); cw=int(sh*9/16)//2*2; x=int((sw-cw)*a.focus)
    v=f"[0:v]crop={cw}:{sh}:{x}:0,scale=1080:1920[v0]"
elif a.layout=="cam":
    x,y,w,h=[int(i) for i in a.cam.split(",")]; sw,sh=probe(a.inp)
    gw=int(sh*0.95*9/16*1.0)  # gameplay crop width for lower 1180px area
    v=(f"[0:v]split[c][g];[c]crop={w}:{h}:{x}:{y},scale=1080:740:force_original_aspect_ratio=increase,crop=1080:740[cam];"
       f"[g]scale=-2:1180,crop=1080:1180[gp];[cam][gp]vstack[v0]")
else: raise SystemExit("bad layout")
draw="[v0]"+"".join(f"drawtext=fontfile={FONT}:textfile={h}:fontsize=76:fontcolor=white:borderw=7:bordercolor=black:x=(w-text_w)/2:y={170+i*95}," for i,h in enumerate(hfs))
draw+=(f"drawtext=fontfile={FONT}:textfile={cf}:fontsize=40:fontcolor=white@0.9:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-330")
if a.srt:
    s=a.srt.replace(":","\\:")
    draw+=f",subtitles={s}:force_style='FontName=Poppins,Bold=1,FontSize=16,Outline=3,Alignment=2,MarginV=230'"
draw+=",fps=30,format=yuv420p[v]"
cmd=["ffmpeg","-y","-hide_banner","-loglevel","error","-ss",str(st),"-t",str(dur),"-i",a.inp,
     "-filter_complex",v+";"+draw,"-map","[v]","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","20",
     "-c:a","aac","-b:a","160k","-ar","48000","-movflags","+faststart",a.out]
subprocess.run(cmd,check=True); print("OK",a.out,f"{dur:.1f}s")
