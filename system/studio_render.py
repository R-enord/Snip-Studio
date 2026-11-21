"""Clip Studio render engine (SnipRiot).

Turns an edit list (EDL) into a finished vertical short, the way the owner's sample works:
  01 HOOK     open on the strongest beat (trailer / gameplay / reaction), hard cuts
  02 CUT      ripple edit: only the parts listed in "clips" survive, back to back
  03 REFRAME  9:16 crop that follows the subject (face for reaction cams, motion for gameplay),
              as smoothed keyframes, or keyframes set by hand in the editor
  04 FINISH   captions (ASS, burned with libass), music under the voice with ducking,
              loudness to -14 LUFS, H.264 export

Usage:  python studio_render.py edit.json            (writes edit["output"])
        python studio_render.py --track clip.mp4 12.0 18.5   (prints reframe keyframes as JSON)

EDL format (all times in seconds of the source file):
{
  "output": "media/studio/short_01.mp4",
  "size": [1080, 1920], "fps": 30, "crf": 20, "preset": "veryfast",
  "clips": [
    {"src": "raw/kai/part1.mp4", "in": 120.0, "out": 124.5,
     "reframe": "auto" | "center" | [{"t": 0.0, "x": 0.42}, ...],   # x = crop centre, 0..1 of source width, t from clip start
     "zoom": 1.0,                 # >1 punches in (sample: "punch into the reaction")
     "layout": "full" | "stack",  # stack = facecam box on top, gameplay below (needs "cam": [x,y,w,h] 0..1)
     "cam": [0.68, 0.15, 0.31, 0.31],
     "speed": 1.0, "volume": 1.0}
  ],
  "captions": [{"t0": 0.0, "t1": 1.2, "text": "CLIP THAT.", "style": "hook"}],   # times on the OUTPUT timeline
  "words": [{"t0": 0.1, "t1": 0.4, "w": "clip"}, ...],      # optional word timings -> karaoke-style captions
  "caption_style": {"font": "Anton", "size": 96, "color": "#FFFFFF", "accent": "#FF4FA3", "y": 0.72, "upper": true},
  "music": {"src": "media/music/track.mp3", "gain_db": -16, "duck": true},
  "loudness": -14
}
Free tools only: ffmpeg (libass), OpenCV (optional; without it "auto" falls back to centre).
"""
import json, math, os, subprocess, sys, tempfile, shutil

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = [os.path.join(HERE, "ui", "fonts"), os.path.join(HERE, "..", "tools", "fonts"), os.path.join(HERE, "fonts")]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: {(r.stderr or '')[-800:]}")
    return r


def probe(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    s = d["streams"][0]
    has_audio = bool(json.loads(run([FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
                                     "stream=index", "-of", "json", path]).stdout).get("streams"))
    return s["width"], s["height"], float(d["format"]["duration"]), has_audio


# ---------------------------------------------------------------- 03 REFRAME: follow the subject
def track(path, t_in, t_out, sample_fps=6.0, prefer="auto"):
    """Crop centre over time as [(t, x)] with x in 0..1 of the source width, t from t_in.
    Faces win when there are any (reaction cams); otherwise the busiest moving area (gameplay)."""
    try:
        import cv2, numpy as np
    except ImportError:
        return [(0.0, 0.5)]
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return [(0.0, 0.5)]
    cap.set(cv2.CAP_PROP_POS_MSEC, t_in * 1000)
    casc = None
    try:   # OpenCV 4.x ships the face model; without it we follow motion only
        casc = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        if casc.empty(): casc = None
    except Exception: casc = None
    prev, pts, last_x = None, [], 0.5
    step = 1.0 / sample_fps
    t = t_in
    while t <= t_out:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        h, w = fr.shape[:2]
        sc = 320.0 / w
        small = cv2.resize(fr, (320, max(1, int(h * sc))))
        g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        x = None
        if casc is not None and prefer in ("auto", "face"):
            faces = casc.detectMultiScale(g, 1.15, 5, minSize=(18, 18))
            if len(faces):
                # biggest face, but stay with the one nearest the last position when sizes are close
                faces = sorted(faces, key=lambda f: (-f[2] * f[3], abs((f[0] + f[2] / 2) / 320 - last_x)))
                fx, fy, fw, fh = faces[0]
                x = (fx + fw / 2) / 320.0
        if x is None and prefer in ("auto", "motion") and prev is not None:
            d = cv2.absdiff(cv2.GaussianBlur(g, (9, 9), 0), cv2.GaussianBlur(prev, (9, 9), 0))
            d[:int(d.shape[0] * 0.08)] = 0            # ignore HUD strips at the very top/bottom
            d[int(d.shape[0] * 0.90):] = 0
            col = d.astype("float32").sum(axis=0)
            if col.sum() > 320 * 40:                  # enough motion to trust
                xs = np.arange(len(col))
                x = float((col * xs).sum() / col.sum()) / 320.0
        prev = g
        if x is not None:
            last_x = x
        pts.append((t - t_in, last_x))
        t += step
    cap.release()
    return smooth(pts)


def smooth(pts, tau=0.6, deadzone=0.04):
    """Camera-operator smoothing: ignore small wobble (deadzone), then ease toward the target."""
    if not pts:
        return [(0.0, 0.5)]
    out, cur, prev_t = [], pts[0][1], pts[0][0]
    for t, x in pts:
        dt = max(1e-3, t - prev_t)
        if abs(x - cur) > deadzone:
            a = 1 - math.exp(-dt / tau)
            cur += (x - cur) * a
        out.append((round(t, 3), round(cur, 4)))
        prev_t = t
    return simplify(out, 0.006)


def simplify(pts, eps):
    """Ramer-Douglas-Peucker: keep only the keyframes that matter (short ffmpeg expressions, editable in the UI)."""
    if len(pts) < 3:
        return pts
    (t0, x0), (t1, x1) = pts[0], pts[-1]
    best, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        t, x = pts[i]
        xi = x0 + (x1 - x0) * ((t - t0) / (t1 - t0) if t1 > t0 else 0)
        if abs(x - xi) > best:
            best, idx = abs(x - xi), i
    if best <= eps:
        return [pts[0], pts[-1]]
    return simplify(pts[:idx + 1], eps)[:-1] + simplify(pts[idx:], eps)


def x_expr(keys, src_w, crop_w):
    """Piecewise-linear ffmpeg expression for the crop's left edge from (t, x-centre) keyframes."""
    def left(xc):
        return max(0.0, min(src_w - crop_w, xc * src_w - crop_w / 2))
    keys = sorted(keys)
    if len(keys) == 1:
        return f"{left(keys[0][1]):.1f}"
    e = f"{left(keys[-1][1]):.1f}"
    for (ta, xa), (tb, xb) in reversed(list(zip(keys, keys[1:]))):
        la, lb = left(xa), left(xb)
        seg = f"{la:.1f}+({lb - la:.1f})*(t-{ta:.3f})/{max(1e-3, tb - ta):.3f}"
        e = f"if(lt(t\\,{tb:.3f})\\,{seg}\\,{e})"
    return f"if(lt(t\\,{keys[0][0]:.3f})\\,{left(keys[0][1]):.1f}\\,{e})"


# ---------------------------------------------------------------- 02 CUT: one segment per clip
def render_clip(c, i, tmp, W, H, fps):
    src = c["src"]
    sw, sh, dur, has_audio = probe(src)
    t_in, t_out = float(c["in"]), min(float(c["out"]), dur)
    speed = float(c.get("speed", 1.0))
    zoom = max(1.0, float(c.get("zoom", 1.0)))
    out = os.path.join(tmp, f"seg_{i:03d}.mp4")
    if c.get("layout") == "stack" and c.get("cam"):
        cx, cy, cw, ch = c["cam"]
        top_h = int(H * 0.42) // 2 * 2
        vf = (f"[0:v]split[a][b];"
              f"[a]crop={int(cw * sw)}:{int(ch * sh)}:{int(cx * sw)}:{int(cy * sh)},scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[cam];"
              f"[b]scale=-2:{H - top_h},crop={W}:{H - top_h}[game];[cam][game]vstack,setsar=1")
    else:
        crop_h = sh / zoom
        crop_w = min(sw, crop_h * W / H)
        reframe = c.get("reframe", "auto")
        if reframe == "auto":
            keys = track(src, t_in, t_out)
        elif reframe == "center":
            keys = [(0.0, 0.5)]
        else:
            keys = [(float(k["t"]), float(k["x"])) for k in reframe]
        c["_keys"] = keys
        y = (sh - crop_h) / 2
        vf = (f"[0:v]crop={crop_w:.0f}:{crop_h:.0f}:'{x_expr(keys, sw, crop_w)}':{y:.0f},"
              f"scale={W}:{H}:flags=lanczos,setsar=1")
    vf += f",setpts=(PTS-STARTPTS)/{speed},fps={fps},format=yuv420p[v]"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t_in:.3f}", "-to", f"{t_out:.3f}", "-i", src]
    if has_audio:
        af = f"[0:a]asetpts=PTS-STARTPTS,atempo={speed},aresample=48000,volume={float(c.get('volume', 1.0))}[a]"
    else:   # silent source: keep the timeline intact with silence
        cmd += ["-f", "lavfi", "-t", f"{(t_out - t_in) / speed:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        af = "[1:a]anull[a]"
    cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "pcm_s16le", "-ac", "2",
            out.replace(".mp4", ".mkv")]
    run(cmd)
    return out.replace(".mp4", ".mkv"), (t_out - t_in) / speed


# ---------------------------------------------------------------- 04 FINISH: captions
def ass_color(hexs, alpha=0):
    hexs = hexs.lstrip("#")
    r, g, b = hexs[0:2], hexs[2:4], hexs[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(path, W, H, captions, words, st):
    font, size = st.get("font", "Anton"), int(st.get("size", 112))
    col, acc = st.get("color", "#FFFFFF"), st.get("accent", "#FF4FA3")
    y = int(H * (1 - float(st.get("y", 0.72))))
    upper = st.get("upper", True)
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: Word,{font},{size},{ass_color(col)},{ass_color(acc)},&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,2,60,60,{y},1",
             f"Style: Hook,{font},{int(size * 1.15)},{ass_color(acc)},{ass_color(col)},&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,8,4,2,60,60,{y},1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    pop = r"{\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"   # quick scale pop on each new caption
    for c in captions or []:
        txt = c["text"].upper() if upper else c["text"]
        style = "Hook" if c.get("style") == "hook" else "Word"
        lines.append(f"Dialogue: 1,{ass_time(c['t0'])},{ass_time(c['t1'])},{style},,0,0,0,,{pop}{txt}")
    # word timings: show 3-word groups, the spoken word in the accent colour
    if words:
        groups, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) == 3 or w["w"].rstrip().endswith((".", "!", "?", ",")):
                groups.append(cur); cur = []
        if cur:
            groups.append(cur)
        for g in groups:
            for k, w in enumerate(g):
                t1 = g[k + 1]["t0"] if k + 1 < len(g) else w["t1"]
                parts = []
                for j, x in enumerate(g):
                    s = x["w"].upper() if upper else x["w"]
                    parts.append((r"{\c" + ass_color(acc) + "}" + s + r"{\c" + ass_color(col) + "}") if j == k else s)
                lines.append(f"Dialogue: 0,{ass_time(w['t0'])},{ass_time(t1)},Word,,0,0,0,,{pop if k == 0 else ''}{' '.join(parts)}")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def font_dir():
    for d in FONTS:
        if os.path.isdir(d):
            return d
    return None


# ---------------------------------------------------------------- the whole edit
def render(edl, progress=None):
    W, H = edl.get("size", [1080, 1920])
    fps = int(edl.get("fps", 30))
    out = edl["output"]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="studio_")
    try:
        segs, total = [], 0.0
        for i, c in enumerate(edl["clips"]):
            p, d = render_clip(c, i, tmp, W, H, fps)
            segs.append(p); total += d
            if progress: progress((i + 1) / (len(edl["clips"]) + 1), f"cut {i + 1}/{len(edl['clips'])}")
        lst = os.path.join(tmp, "list.txt")
        open(lst, "w").write("".join(f"file '{p}'\n" for p in segs))
        joined = os.path.join(tmp, "joined.mkv")
        run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
        ass = os.path.join(tmp, "caps.ass")
        write_ass(ass, W, H, edl.get("captions"), edl.get("words"), edl.get("caption_style", {}))
        fd = font_dir()
        sub = f"subtitles=filename='{ass.replace(chr(92), '/').replace(':', chr(92) + ':')}'" + (
            f":fontsdir='{fd.replace(chr(92), '/').replace(':', chr(92) + ':')}'" if fd else "")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", joined]
        m = edl.get("music")
        lufs = float(edl.get("loudness", -14))
        if m and m.get("src") and os.path.exists(m["src"]):
            cmd += ["-stream_loop", "-1", "-i", m["src"]]
            g = float(m.get("gain_db", -16))
            if m.get("duck", True):   # music dips while the clip's own audio is loud
                af = (f"[1:a]volume={g}dB,atrim=0:{total:.3f},aresample=48000[mus];[0:a]asplit[v1][v2];"
                      f"[mus][v2]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=400[duck];"
                      f"[v1][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I={lufs}:TP=-1.5:LRA=11[a]")
            else:
                af = (f"[1:a]volume={g}dB,atrim=0:{total:.3f}[mus];[0:a][mus]amix=inputs=2:duration=first:normalize=0,"
                      f"loudnorm=I={lufs}:TP=-1.5:LRA=11[a]")
        else:
            af = f"[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11[a]"
        cmd += ["-filter_complex", f"[0:v]{sub}[v];{af}", "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", edl.get("preset", "veryfast"), "-crf", str(edl.get("crf", 20)),
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-t", f"{total:.3f}", out]
        run(cmd)
        if progress: progress(1.0, "exported")
        return {"output": out, "duration": round(total, 2),
                "keys": [c.get("_keys") for c in edl["clips"]]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--track":
        print(json.dumps(track(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))))
    else:
        edl = json.load(open(sys.argv[1], encoding="utf-8"))
        print(json.dumps(render(edl, lambda p, m: print(f"{p * 100:.0f}% {m}", file=sys.stderr))))
