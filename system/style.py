"""SnipRiot house style ("Split-cam"), used for every final clip.

  1080x1920 · top 640px = streamer facecam · bottom 1280px = gameplay
  title: sentence case, max 2 lines, black Inter SemiBold in a white rounded box at the top of the gameplay, emoji allowed
  credit pill on the facecam ("🎥 IShowSpeed"), small @SnipRiotGTA mark bottom-left (above the platform caption area), nothing in TikTok's bottom/right UI zones
  audio: streamer audio loudness-normalised + low royalty-free music bed that ducks under the voice
  no facecam in the source -> full-height gameplay with the same title box

  subtitles (--subs): what the streamer says, 1-3 words at a time, white with a dark outline, lower-middle of the gameplay
    (clear of the title box, the watermark and the platform caption area); strong swear words are masked

Usage: python style.py SOURCE.mp4 OUT.mp4 --title "Fighting cops in GTA 6 💀" --credit "IShowSpeed"
          [--cam x,y,w,h] [--game x,y,w,h] (fractions 0-1 of the source frame) [--start s --end s] [--music file|none] [--subs]
"""
import argparse, glob, json, os, random, subprocess, tempfile
from PIL import Image, ImageDraw, ImageFont
import campaigns

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FONTS = os.path.join(ROOT, "brand", "fonts")
MUSIC = os.path.join(ROOT, "brand", "music")
W, H, CAM_H = 1080, 1920, 640
TEXT_FONT = os.path.join(FONTS, "Inter-SemiBold.otf")
EMOJI_FONT = os.path.join(FONTS, "NotoColorEmoji.ttf")
HANDLE = ""  # owner rule 2026-11-23: no watermark

def is_emoji(ch):
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0x200D, 0xFE0F)

def runs(text):
    out = []
    for ch in text:
        e = is_emoji(ch)
        if out and out[-1][0] == e: out[-1][1] += ch
        else: out.append([e, ch])
    return out

def emoji_img(seq, px):
    f = ImageFont.truetype(EMOJI_FONT, 109)  # bitmap emoji font only renders at 109
    im = Image.new("RGBA", (160 * max(1, len(seq)), 160), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), seq, font=f, embedded_color=True)
    im = im.crop(im.getbbox() or (0, 0, 1, 1))
    return im.resize((max(1, int(im.width * px / im.height)), px), Image.LANCZOS)

def measure(text, font, px):
    w = 0
    for e, s in runs(text):
        w += emoji_img(s, int(px * 0.95)).width + 4 if e else font.getlength(s)
    return w

def wrap(text, font, px, maxw):
    words = text.split()
    if measure(text, font, px) <= maxw: return [text]
    # two balanced lines when that fits (no lonely last word)
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        wa, wb = measure(a, font, px), measure(b, font, px)
        if wa <= maxw and wb <= maxw and (best is None or max(wa, wb) < best[0]): best = (max(wa, wb), [a, b])
    if best: return best[1]
    return _greedy(text, font, px, maxw)

def _greedy(text, font, px, maxw):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if measure(t, font, px) <= maxw or not cur: cur = t
        else: lines.append(cur); cur = wd
    lines.append(cur)
    return lines

def draw_rich(im, xy, text, font, px, fill):
    x, y = xy; d = ImageDraw.Draw(im)
    for e, s in runs(text):
        if e:
            em = emoji_img(s, int(px * 0.95)); im.alpha_composite(em, (int(x) + 2, int(y + px * 0.12))); x += em.width + 4
        else:
            d.text((x, y), s, font=font, fill=fill); x += font.getlength(s)

def title_png(title, path, px=56):
    font = ImageFont.truetype(TEXT_FONT, px)
    lines = wrap(title, font, px, 860)
    while len(lines) > 2 and px > 40:
        px -= 4; font = ImageFont.truetype(TEXT_FONT, px); lines = wrap(title, font, px, 860)
    lh = int(px * 1.22)
    im = Image.new("RGBA", (W, lh * len(lines) + 60), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    # one rounded box per line, like TikTok's classic text style
    for i, ln in enumerate(lines):
        tw = measure(ln, font, px); x0 = (W - tw) / 2; y0 = 10 + i * lh
        d.rounded_rectangle((x0 - 22, y0 - 8, x0 + tw + 22, y0 + lh + 2), radius=16, fill=(255, 255, 255, 255))
    for i, ln in enumerate(lines):
        tw = measure(ln, font, px); draw_rich(im, ((W - tw) / 2, 10 + i * lh - px * 0.06), ln, font, px, (0, 0, 0, 255))
    im.save(path)
    return im.height

def watermark(im, d=None):
    """Small @SnipRiotGTA mark, bottom-left, kept above the caption/username area TikTok, Shorts and Reels draw at the bottom."""
    from PIL import ImageFilter
    if not HANDLE: return                        # campaign clips: no watermark allowed
    fh = ImageFont.truetype(TEXT_FONT, 25)
    x, y = 34, H - 530
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0)); ImageDraw.Draw(sh).text((x + 2, y + 2), HANDLE, font=fh, fill=(0, 0, 0, 120))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(2)))
    ImageDraw.Draw(im).text((x, y), HANDLE, font=fh, fill=(255, 255, 255, 140))

def overlay_png(credit, path, has_cam):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = ImageFont.truetype(TEXT_FONT, 30)
    label = f"🎥 {credit}"
    tw = measure(label, f, 30)
    y = CAM_H - 64 if has_cam else 150
    d.rounded_rectangle((28, y, 28 + tw + 28, y + 46), radius=23, fill=(0, 0, 0, 150))
    draw_rich(im, (42, y + 5), label, f, 30, (255, 255, 255, 255))
    watermark(im)
    im.save(path)

SUB_Y = 1235          # vertical centre of the subtitle line
SUB_PX = 64
SUB_LEAD = 0.35       # show each line this much before the words are spoken, so viewers read along instead of lagging
MODEL_DIRS = [os.path.expanduser("~/mnt/SnipRiot/tools/models/small.en"), os.path.expanduser("~/SnipRiot/tools/models/small.en")]
MASK = {"fuck": "f*ck", "fucking": "f*cking", "fuckin": "f*ckin", "fucked": "f*cked", "motherfucker": "m*therf*cker", "shit": "sh*t",
        "bitch": "b*tch", "nigga": "n***a", "niggas": "n***as", "nigger": "n*****", "pussy": "p*ssy", "dick": "d*ck", "cunt": "c*nt"}

def model_dir():
    for d in MODEL_DIRS:
        if os.path.exists(os.path.join(d, "model.bin")):
            if os.name == "nt": return d                 # on the PC itself the folder is local already
            cache = "/tmp/snipriot-whisper-small.en"   # local copy loads much faster than the shared folder
            if not os.path.exists(os.path.join(cache, "model.bin")):
                import shutil; shutil.copytree(d, cache + ".part", dirs_exist_ok=True); os.replace(cache + ".part", cache)
            return cache
    raise SystemExit("subtitle model not found (tools/models/small.en)")

def transcribe(src, s0, d, ffmpeg="ffmpeg"):
    from faster_whisper import WhisperModel
    wav = tempfile.mktemp(suffix=".wav")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{s0:.2f}", "-t", f"{d:.2f}", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
    m = WhisperModel(model_dir(), device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4) // 2))
    import wave, numpy as np   # hand the audio over as samples: avoids PyAV version differences on Windows
    with wave.open(wav, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = m.transcribe(audio, language="en", word_timestamps=True, vad_filter=True, beam_size=1, condition_on_previous_text=False)
    words = []
    for sg in segs:
        if sg.no_speech_prob > 0.6: continue
        for w in sg.words or []:
            t = w.word.strip()
            if t and w.probability > 0.25: words.append((w.start, w.end, t))
    os.remove(wav)
    return words

def clean(t):
    core = t.strip(".,!?;:\"'").lower()
    if core in MASK: t = t.lower().replace(core, MASK[core])
    return t.strip(",;:")

def chunks(words, d):
    out, cur = [], []
    for i, (a, b, t) in enumerate(words):
        if cur and (len(cur) >= 3 or a - cur[-1][1] > 0.6 or sum(len(x[2]) for x in cur) + len(t) > 16): out.append(cur); cur = []
        cur.append((a, b, clean(t)))
        if t[-1:] in ".!?": out.append(cur); cur = []
    if cur: out.append(cur)
    res = []
    for k, c in enumerate(out):
        a, b = c[0][0] - SUB_LEAD, c[-1][1] + 0.15
        if k + 1 < len(out): b = min(b, out[k + 1][0][0] - SUB_LEAD)
        txt = " ".join(x[2] for x in c).strip()
        if txt and b - a > 0.12: res.append((max(0, a), min(d, b), txt))
    return res

def sub_png(text, path):
    f = ImageFont.truetype(TEXT_FONT, SUB_PX)
    tw = int(f.getlength(text)) + 40; th = SUB_PX + 40
    im = Image.new("RGBA", (min(tw, W - 60), th), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.text((im.width / 2, th / 2), text, font=f, anchor="mm", fill=(255, 255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0, 235))
    im.save(path); return im.size

def pick_music(seed):
    tracks = sorted(glob.glob(os.path.join(MUSIC, "*.mp3")) + glob.glob(os.path.join(MUSIC, "*.m4a")) + glob.glob(os.path.join(MUSIC, "*.wav")))
    return random.Random(seed).choice(tracks) if tracks else None

def crop_expr(box, sw, sh, ow, oh):
    x, y, w, h = box
    cw, ch = w * sw, h * sh
    # cover the output panel: trim the box to the panel's aspect ratio around its centre
    if cw / ch > ow / oh: nw, nh = ch * ow / oh, ch
    else: nw, nh = cw, cw * oh / ow
    cx, cy = x * sw + cw / 2, y * sh + ch / 2
    return f"crop={int(nw)//2*2}:{int(nh)//2*2}:{int(max(0,cx-nw/2))}:{int(max(0,cy-nh/2))},scale={ow}:{oh}"

def render(src, out, title, credit, cam=None, game=None, start=None, end=None, music="auto", subs=False, ffmpeg="ffmpeg", ffprobe="ffprobe",
           logo=None, logo_width=540, logo_margin=0):
    pr = json.loads(subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                                    "-of", "json", src], capture_output=True, text=True).stdout)
    sw, sh = pr["streams"][0]["width"], pr["streams"][0]["height"]; dur = float(pr["format"]["duration"])
    s0 = start or 0; d = (end or dur) - s0
    tmp = tempfile.mkdtemp()
    tp, op = os.path.join(tmp, "title.png"), os.path.join(tmp, "ov.png")
    title_png(title, tp); overlay_png(credit, op, bool(cam))
    lx = ly = None
    lg = None
    if logo:                                  # campaign logo: bottom-right, scaled to logo_width, aspect kept
        lw0, lh0 = Image.open(logo).size
        ctw = measure(f"🎥 {credit}", ImageFont.truetype(TEXT_FONT, 30), 30)
        cy = CAM_H - 64 if cam else 150
        lw, lh, lx, ly = campaigns.logo_fit(lw0, lh0, logo_width, cam_h=CAM_H if cam else 0, credit_box=(28, cy, 28 + ctw + 28, cy + 46), margin=logo_margin)
        lg = os.path.join(tmp, "logo.png"); Image.open(logo).convert("RGBA").resize((lw, lh), Image.LANCZOS).save(lg)
    game = game or ([0.2, 0, 0.6, 1] if not cam else [0, 0, 1, 1])
    if cam:
        v = (f"[0:v]split[a][b];[a]{crop_expr(cam, sw, sh, W, CAM_H)}[cam];[b]{crop_expr(game, sw, sh, W, H - CAM_H)}[gm];"
             f"[cam][gm]vstack[base];[base][1:v]overlay=0:{CAM_H + 26}[t];[t][2:v]overlay=0:0[o0]")
    else:
        v = (f"[0:v]{crop_expr(game, sw, sh, W, H)}[base];[base][1:v]overlay=0:230[t];[t][2:v]overlay=0:0[o0]")
    inputs = ["-ss", f"{s0:.2f}", "-t", f"{d:.2f}", "-i", src, "-i", tp, "-i", op]
    trk = pick_music(out) if music == "auto" else (None if music in (None, "none") else music)
    lines = []   # subtitles are no longer shown on clips (owner 2026-11-24); transcribe() is not called
    first = 4 if trk else 3; last = "o0"
    logo_idx = first + len(lines) if logo else None   # input index after the subtitle PNGs
    for k, (a, b, txt) in enumerate(lines):
        sp = os.path.join(tmp, f"s{k}.png"); w_, h_ = sub_png(txt, sp)
        v += f";[{last}][{first + k}:v]overlay={(W - w_) // 2}:{SUB_Y - h_ // 2}:enable='between(t,{a:.2f},{b:.2f})'[o{k + 1}]"; last = f"o{k + 1}"
    if logo_idx is not None:                  # last layer, so nothing covers the logo
        v += f";[{last}][{logo_idx}:v]overlay={lx}:{ly}[ologo]"; last = "ologo"
    v += f";[{last}]fps=30,format=yuv420p[v]"
    sub_inputs = sum((["-i", os.path.join(tmp, f"s{k}.png")] for k in range(len(lines))), [])
    fade = f"afade=t=in:d=0.25,afade=t=out:st={max(0, d - 0.4):.2f}:d=0.4"
    if trk:
        inputs += ["-stream_loop", "-1", "-i", trk]
    inputs += sub_inputs
    if logo:
        inputs += ["-i", lg]
    if trk:
        a = (f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit[voice][key];"
             f"[3:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.16,afade=t=in:d=1,afade=t=out:st={max(0, d - 1.2):.2f}:d=1.2[bed];"
             f"[bed][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
             f"[voice][duck]amix=inputs=2:normalize=0:duration=first,{fade}[a]")
    else:
        a = f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,{fade}[a]"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + inputs + ["-filter_complex", v + ";" + a, "-map", "[v]", "-map", "[a]",
           "-t", f"{d:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    return {"out": out, "music": os.path.basename(trk) if trk else None, "length_s": round(d, 1), "subtitles": len(lines),
            "transcript": " ".join(x[2] for x in lines), "logo": bool(logo)}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out"); ap.add_argument("--title", required=True); ap.add_argument("--credit", required=True)
    fl = lambda s: [float(x) for x in s.split(",")]
    ap.add_argument("--cam", type=fl); ap.add_argument("--game", type=fl)
    ap.add_argument("--start", type=float); ap.add_argument("--end", type=float); ap.add_argument("--music", default="auto")
    ap.add_argument("--subs", action="store_true")
    ap.add_argument("--logo", help="campaign logo PNG, placed bottom-right and scaled to --logo-width")
    ap.add_argument("--logo-width", type=int, default=540)
    ap.add_argument("--logo-margin", type=int, default=0)
    ap.add_argument("--handle", default=HANDLE, help="watermark text; 'none' for no watermark")
    a = ap.parse_args()
    HANDLE = "" if a.handle.lower() == "none" else a.handle
    print(json.dumps(render(a.src, a.out, a.title, a.credit, a.cam, a.game, a.start, a.end, a.music, a.subs, logo=a.logo, logo_width=a.logo_width, logo_margin=a.logo_margin), ensure_ascii=False))
