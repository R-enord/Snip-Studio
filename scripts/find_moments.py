#!/usr/bin/env python3
"""Find hype moments in a long stream by loudness spikes (streamer yelling, crashes, music hits).
Usage: python3 find_moments.py IN.mp4 [top_n=25] [min_gap_s=45]
Prints candidate timestamps (HH:MM:SS) ranked by how far above the local average they are,
and writes contact sheets of frames around each peak to <IN>_moments/ for review."""
import subprocess, sys, re, os, statistics
f=sys.argv[1]; top=int(sys.argv[2]) if len(sys.argv)>2 else 25; gap=float(sys.argv[3]) if len(sys.argv)>3 else 45
p=subprocess.run(["ffmpeg","-hide_banner","-nostats","-i",f,"-vn","-af","aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level","-f","null","-"],capture_output=True,text=True).stderr
vals=[float(x) if x not in("-inf","inf") else -90.0 for x in re.findall(r"RMS_level=(\S+)",p)]
if not vals: sys.exit("no audio")
win=60; scores=[]
for i,v in enumerate(vals):
    lo=max(0,i-win); hi=min(len(vals),i+win); base=statistics.median(vals[lo:hi])
    scores.append((v-base,i))
scores.sort(reverse=True); picks=[]
for sc,i in scores:
    if all(abs(i-j)>gap for _,j in picks): picks.append((sc,i))
    if len(picks)>=top: break
fmt=lambda s:f"{s//3600:02d}:{s%3600//60:02d}:{s%60:02d}"
out=os.path.splitext(f)[0]+"_moments"; os.makedirs(out,exist_ok=True)
for rank,(sc,i) in enumerate(sorted(picks,key=lambda x:x[1]),1):
    print(f"{rank:02d}  {fmt(i)}  +{sc:.1f} dB")
    s=max(0,i-8)
    subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-ss",str(s),"-t","16","-i",f,"-vf","fps=0.5,scale=480:-2,tile=4x2","-frames:v","1",os.path.join(out,f"{rank:02d}_{fmt(i).replace(':','-')}.jpg")])
print("sheets:",out)
