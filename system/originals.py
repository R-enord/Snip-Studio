"""SnipRiot originals renderer (the 2 daily GTA 6 videos: tips, how-tos, facts, things you missed).
Style from media/official/originals-style (NOT the streamer-clip style):
  full-screen 9:16 gameplay with fast cuts, AI voice narration (Piper, free/offline), big ALL-CAPS captions 1-3 words
  at a time synced to the voice with keywords in colour, numbered labels at the top, info cards (stats, comparisons,
  big numbers), low music ducked under the voice, small @SnipRiotGTA watermark bottom-left, footage credit.

Usage: python originals.py SPEC.json OUT.mp4
SPEC = {
  "title": "5 things GTA 6 NPCs do", "credit": "Footage: IShowSpeed, Kai Cenat",
  "voice": "en_US-ryan-high", "speed": 1.0,
  "lines": [
    {"say": "NPCs in GTA 6 *remember* your face.",            # *green*  ^yellow^  ~red~  (markers aren't spoken)
     "clips": [{"src": "media/candidates/kai/2026-11-19/x.mp4", "start": 3.0, "focus": 0.5}],   # src relative to data root
     "label": "1. THEY REMEMBER YOU",                          # optional, small text at the top
     "card": {"type": "stat", "title": "HOW LONG TO BEAT IT?", "rows": [["GTA 5", "32H"], ["GTA 6", "50H+"]]}},
    {"say": "...", "clips": [...], "card": {"type": "big", "text": "$0", "sub": "YOUR CASH"}}
  ]}
Each line's clips share the line's narration time (fast cuts). focus = horizontal position of the 9:16 crop (0-1)."""
import argparse, json, os, random, re, subprocess, sys, tempfile, wave
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
FONTS = os.path.join(ROOT, "brand", "fonts"); MUSIC = os.path.join(ROOT, "brand", "music")
DATA_ROOT = os.path.expanduser("~/SnipRiot") if os.name == "nt" else os.path.expanduser("~/mnt/SnipRiot")
VOICES = [os.path.join(DATA_ROOT, "tools", "voices"), os.path.expanduser("~/SnipRiot/tools/voices")]
W, H, FPS = 1080, 1920, 30
HANDLE = "@SnipRiotGTA"
CAP_Y = 1200                  # caption line centre (lower middle, clear of TikTok/Shorts bottom UI)
COL = {"*": (60, 255, 90), "^": (255, 214, 0), "~": (255, 60, 60)}

def font(size, heavy=True):
    for p in ([os.path.join(FONTS, "Poppins-Bold.ttf"), r"C:\Windows\Fonts\ariblk.ttf"] if heavy else []) + \
             [os.path.join(FONTS, "Inter-SemiBold.otf"), os.path.join(FONTS, "Poppins-Bold.ttf")]:
        if os.path.exists(p): return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def words_of(say):
    """[(word, colour or None)] from a line with *green* ^yellow^ ~red~ markers."""
    out, colour = [], None
    for tok in re.findall(r"[*^~]|[^\s*^~]+", say):
        if tok in COL: colour = None if colour == COL[tok] else COL[tok]; continue
        if out and re.fullmatch(r"[.,!?:;'\")]+", tok): out[-1] = (out[-1][0] + tok, out[-1][1]); continue   # "^crime^." -> "CRIME."
        out.append((tok, colour))
    return out

def spoken(say): return re.sub(r"[*^~]", "", say).strip()

# ---------------- voice ----------------
def voice_path(name):
    for d in VOICES:
        p = os.path.join(d, name + ".onnx")
        if os.path.exists(p): return p
    raise SystemExit(f"voice {name} not found in tools/voices")

def tts(lines, name, speed, tmp):
    """One wav per line (so captions and cuts line up exactly). Returns [(path, seconds)]."""
    if os.environ.get("SNIPRIOT_FAKE_TTS"):   # test mode without a voice model
        out = []
        for i, l in enumerate(lines):
            d = max(1.2, len(spoken(l["say"])) / 15.0); p = os.path.join(tmp, f"v{i}.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={d:.2f}",
                            "-ar", "22050", "-ac", "1", p], check=True); out.append((p, d))
        return out
    if name.startswith("kokoro:"):           # Kokoro (natural voice): "kokoro:am_michael"
        import numpy as np
        from kokoro_onnx import Kokoro
        kd = os.path.join(DATA_ROOT, "tools", "kokoro")
        k = Kokoro(os.path.join(kd, "kokoro-v1.0.int8.onnx"), os.path.join(kd, "voices-v1.0.bin"))
        out = []
        for i, l in enumerate(lines):
            p = os.path.join(tmp, f"v{i}.wav")
            samples, sr = k.create(spoken(l["say"]), voice=name.split(":", 1)[1], speed=max(0.7, min(1.4, speed)), lang="en-us")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            with wave.open(p, "wb") as wf: wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr); wf.writeframes(pcm)
            out.append((p, len(samples) / float(sr)))
        return out
    from piper import PiperVoice, SynthesisConfig
    v = PiperVoice.load(voice_path(name)); cfg = SynthesisConfig(length_scale=1.0 / max(0.7, min(1.4, speed)))
    out = []
    for i, l in enumerate(lines):
        p = os.path.join(tmp, f"v{i}.wav")
        with wave.open(p, "wb") as wf: v.synthesize_wav(spoken(l["say"]), wf, syn_config=cfg)
        with wave.open(p, "rb") as wf: d = wf.getnframes() / float(wf.getframerate())
        out.append((p, d))
    return out

# ---------------- pictures ----------------
def caption_chunks(words, t0, d):
    """Split a line's words into 1-3 word chunks, timed by letter count across the line's speech."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 3 or sum(len(x[0]) for x in cur) >= 13 or w[0][-1:] in ".,!?:": chunks.append(cur); cur = []
    if cur: chunks.append(cur)
    total = sum(len(w[0]) + 1 for w in words) or 1; t = t0; out = []
    for c in chunks:
        dt = d * sum(len(w[0]) + 1 for w in c) / total
        out.append((t - 0.08, t + dt - 0.12, c)); t += dt    # appear a moment before the word; gone before the next chunk
    return out

def caption_png(chunk, path, scale=1.0):
    f = font(int(96 * scale)); sp = f.getlength(" ")
    txt = [(w.upper(), c) for w, c in chunk]
    width = int(sum(f.getlength(w) for w, _ in txt) + sp * (len(txt) - 1)) + 60
    if width > W - 40:
        f = font(int(96 * scale * (W - 100) / width)); sp = f.getlength(" ")
        width = int(sum(f.getlength(w) for w, _ in txt) + sp * (len(txt) - 1)) + 60
    hgt = f.size + 60
    im = Image.new("RGBA", (width, hgt), (0, 0, 0, 0)); d = ImageDraw.Draw(im); x = 30
    for w, c in txt:
        d.text((x + 5, hgt // 2 + 7), w, font=f, anchor="lm", fill=(0, 0, 0, 150), stroke_width=9, stroke_fill=(0, 0, 0, 150))
        d.text((x, hgt // 2), w, font=f, anchor="lm", fill=(c or (255, 255, 255)) + (255,), stroke_width=9, stroke_fill=(0, 0, 0, 255))
        x += f.getlength(w) + sp
    im = italic(im, 0.12); im.save(path); return im.size

def label_png(text, path):
    f = font(54); w = int(f.getlength(text.upper())) + 60
    im = Image.new("RGBA", (w, 110), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.text((30, 55), text.upper(), font=f, anchor="lm", fill=(255, 214, 0, 255), stroke_width=7, stroke_fill=(0, 0, 0, 255))
    im.save(path); return im.size

def card_png(card, path):
    pw = 880; pad = 40; neon = (255, 60, 200, 255); fill = (22, 10, 40, 225)
    if card.get("type") == "big":
        f1, f2 = font(190), font(44, heavy=False)
        ph = 330 + (60 if card.get("sub") else 0)
        im = Image.new("RGBA", (pw, ph), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
        d.rounded_rectangle((4, 4, pw - 5, ph - 5), 34, fill=fill, outline=neon, width=6)
        y = 40
        if card.get("sub"): d.text((pw // 2, y + 20), card["sub"].upper(), font=f2, anchor="mm", fill=(255, 255, 255, 220)); y += 60
        d.text((pw // 2, y + 125), card["text"], font=f1, anchor="mm", fill=(255, 70, 110, 255), stroke_width=4, stroke_fill=(60, 0, 30, 255))
    else:
        rows = card.get("rows") or []; ft, fl, fv = font(40, heavy=False), font(56, heavy=False), font(70)
        ph = pad * 2 + (70 if card.get("title") else 0) + 100 * len(rows)
        im = Image.new("RGBA", (pw, ph), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
        d.rounded_rectangle((4, 4, pw - 5, ph - 5), 34, fill=fill, outline=neon, width=6)
        y = pad
        if card.get("title"): d.text((pad, y + 22), card["title"].upper(), font=ft, anchor="lm", fill=(255, 214, 0, 255)); y += 70
        for i, (k, v) in enumerate(rows):
            d.text((pad, y + 50), str(k), font=fl, anchor="lm", fill=(255, 255, 255, 255))
            d.text((pw - pad, y + 50), str(v), font=fv, anchor="rm", fill=(255, 214, 0, 255) if i == len(rows) - 1 else (255, 120, 220, 255))
            y += 100
    im.save(path); return im.size

def static_png(credit, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    fw = font(25, heavy=False)
    if HANDLE: d.text((36, H - 530), HANDLE, font=fw, fill=(255, 255, 255, 140), stroke_width=2, stroke_fill=(0, 0, 0, 90))
    if credit:
        fc = font(28, heavy=False); d.text((36, 70), credit, font=fc, fill=(255, 255, 255, 200), stroke_width=3, stroke_fill=(0, 0, 0, 160))
    im.save(path)

# ---------------- video ----------------
def probe(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", src], capture_output=True, text=True)
    j = json.loads(r.stdout); s = j["streams"][0]
    return s["width"], s["height"], float(j["format"]["duration"])

ZOOM, TOP = 1.22, 0.025   # zoom in a little and keep the top: crops off the game's own subtitles at the bottom

def segment(src, start, dur, focus, out, kb=0):
    """One shot: 9:16 crop around the action (centre of the game image), colour boost, slow push-in (Ken Burns)."""
    sw, sh, sd = probe(src)
    start = max(0.0, min(start, max(0.0, sd - dur - 0.05)))
    scale_h = int(round(H * ZOOM / 2) * 2); scale_w = int(round(sw * scale_h / sh / 2) * 2)
    if scale_w < W: scale_w, scale_h = W, int(round(sh * W / sw / 2) * 2)
    x = int((scale_w - W) * max(0.0, min(1.0, focus)))
    y = min(scale_h - H, int(scale_h * TOP))
    n = max(2, int(dur * FPS))
    z0, z1 = (1.0, 1.07) if kb % 2 == 0 else (1.07, 1.0)      # alternate push-in / pull-out for movement
    zoom = f"zoompan=z='{z0}+({z1}-{z0})*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}"
    vf = (f"scale={scale_w}:{scale_h},crop={W}:{H}:{x}:{y},setsar=1,eq=saturation=1.28:contrast=1.06:brightness=0.01,"
          f"{zoom},tpad=stop_mode=clone:stop_duration={dur:.2f}")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", src, "-t", f"{dur:.3f}", "-an", "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), out], check=True)

# ---------------- motion graphics ----------------
EMOJI_FONT = os.path.join(FONTS, "NotoColorEmoji.ttf")

def italic(im, shear=0.2):
    w, h = im.size; extra = int(h * shear)
    return im.transform((w + extra, h), Image.AFFINE, (1, shear, -extra, 0, 1, 0), resample=Image.BICUBIC)

def text_img(text, size, fill, stroke=8, shadow=True, gradient=None, slant=True):
    while size > 60 and font(size).getlength(text) + 40 + size * 0.35 > W - 50: size -= 6     # fit the screen width
    f = font(size); w = int(f.getlength(text)) + 40; h = int(size * 1.35) + 30
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    if shadow:
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0)); ImageDraw.Draw(sh).text((26, 22), text, font=f, fill=(0, 0, 0, 170))
        from PIL import ImageFilter; im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(6)))
    if gradient:
        mask = Image.new("L", im.size, 0); ImageDraw.Draw(mask).text((20, 14), text, font=f, fill=255)
        grad = Image.new("RGBA", im.size); gd = ImageDraw.Draw(grad)
        (r1, g1, b1), (r2, g2, b2) = gradient
        for yy in range(h):
            k = yy / max(1, h - 1); gd.line([(0, yy), (w, yy)], fill=(int(r1 + (r2 - r1) * k), int(g1 + (g2 - g1) * k), int(b1 + (b2 - b1) * k), 255))
        d.text((20, 14), text, font=f, fill=(0, 0, 0, 0), stroke_width=stroke, stroke_fill=(20, 10, 30, 255))
        im.paste(grad, (0, 0), mask)
    else:
        d.text((20, 14), text, font=f, fill=fill + (255,), stroke_width=stroke, stroke_fill=(15, 10, 25, 255))
    return italic(im) if slant else im

def badge_png(path):
    """Our own GTA 6 badge (not Rockstar's logo): gradient tile with heavy white text."""
    bw, bh = 700, 400
    im = Image.new("RGBA", (bw + 40, bh + 40), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    grad = Image.new("RGBA", (bw, bh)); gd = ImageDraw.Draw(grad)
    for yy in range(bh):
        k = yy / (bh - 1)
        c = (int(110 + 145 * k), int(70 + 60 * k), int(230 - 150 * k), 255)      # purple -> pink -> orange
        gd.line([(0, yy), (bw, yy)], fill=c)
    mask = Image.new("L", (bw, bh), 0); ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw - 1, bh - 1), 46, fill=255)
    d.rounded_rectangle((14, 14, bw + 26, bh + 26), 50, fill=(20, 8, 40, 255))
    im.paste(grad, (20, 20), mask)
    f1 = font(230); f2 = font(58, heavy=False)
    d.text((20 + bw // 2, 20 + bh // 2 - 10), "GTA 6", font=f1, anchor="mm", fill=(255, 255, 255, 255), stroke_width=10, stroke_fill=(40, 10, 60, 255))
    d.text((20 + bw // 2, 20 + bh - 36), "VICE CITY · LEONIDA", font=f2, anchor="mm", fill=(255, 255, 255, 235))
    im = im.rotate(-4, resample=Image.BICUBIC, expand=True); im.save(path); return im.size

def label_pill(text, path, kind="num"):
    """Numbered badge or red-X pill for list points; slides in from the left."""
    f = font(74); tw = int(f.getlength(text.upper()))
    ph = 140; icon = 140; pw = tw + icon + 80
    im = Image.new("RGBA", (pw + 20, ph + 20), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((10, 10, pw + 10, ph + 10), 52, fill=(15, 10, 30, 235), outline=(255, 255, 255, 60), width=3)
    cx, cy = 10 + icon // 2 + 4, 10 + ph // 2
    if kind == "never":
        d.ellipse((cx - 54, cy - 54, cx + 54, cy + 54), fill=(235, 40, 60, 255))
        d.line((cx - 24, cy - 24, cx + 24, cy + 24), fill=(255, 255, 255, 255), width=14)
        d.line((cx - 24, cy + 24, cx + 24, cy - 24), fill=(255, 255, 255, 255), width=14)
        tcol = (255, 90, 110, 255)
    else:
        d.ellipse((cx - 54, cy - 54, cx + 54, cy + 54), fill=(60, 230, 110, 255))
        num = (re.match(r"\s*(\d+)", text) or [None, ""])[1] or "★"
        d.text((cx, cy + 2), num, font=font(74), anchor="mm", fill=(10, 30, 15, 255))
        text = re.sub(r"^\s*\d+\.?\s*", "", text)
        tcol = (255, 255, 255, 255)
    d.text((10 + icon + 20, cy + 2), text.upper(), font=f, anchor="lm", fill=tcol)
    im.save(path); return im.size

def emoji_png(ch, path, size=250):
    try:
        f = ImageFont.truetype(EMOJI_FONT, 109)
        im = Image.new("RGBA", (160, 160), (0, 0, 0, 0)); ImageDraw.Draw(im).text((80, 80), ch, font=f, embedded_color=True, anchor="mm")
        bb = im.getbbox()
        if not bb: return None
        im = im.crop(bb).resize((size, int(size * (bb[3] - bb[1]) / max(1, bb[2] - bb[0]))), Image.LANCZOS)
        im.save(path); return im.size
    except Exception:
        return None

def stamp_png(text, path):
    f = font(150); tw = int(f.getlength(text)) + 80
    im = Image.new("RGBA", (tw, 230), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, tw - 8, 222), 24, outline=(235, 30, 50, 255), width=14)
    d.text((tw // 2, 115), text, font=f, anchor="mm", fill=(235, 30, 50, 255))
    im = im.rotate(12, resample=Image.BICUBIC, expand=True); im.save(path); return im.size

def solid_png(path, rgba):
    Image.new("RGBA", (W, H), rgba).save(path)

def bar_png(path, k, n):
    """Progress bar at the very top: k of n list points done."""
    im = Image.new("RGBA", (W, 26), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    gapw = 8; segw = (W - 60 - gapw * (n - 1)) / n
    for i in range(n):
        x0 = 30 + i * (segw + gapw)
        d.rounded_rectangle((x0, 6, x0 + segw, 20), 7, fill=(60, 230, 110, 255) if i < k else (255, 255, 255, 70))
    im.save(path)

def ease(a, b, t0, dur=0.28):
    """ffmpeg expression: slide from a to b starting at t0 (ease-out cubic)."""
    return f"'{b}+({a}-({b}))*pow(1-min(1,max(0,(t-{t0:.3f})/{dur})),3)'"

def render(spec, out):
    tmp = tempfile.mkdtemp(); lines = spec["lines"]
    voice = tts(lines, spec.get("voice", "en_US-ryan-high"), float(spec.get("speed", 1.0)), tmp)
    gap = 0.12; t = 0.25; timeline = []
    for (vp, vd), l in zip(voice, lines):
        timeline.append((t, vd, vp, l)); t += vd + gap
    total = t + 0.4
    # picture: every line's clips share its time (fast cuts)
    segs = []
    for i, (t0, vd, _, l) in enumerate(timeline):
        span = vd + gap + (0.25 if i == 0 else 0) + (0.4 if i == len(timeline) - 1 else 0)
        clips = l.get("clips") or []
        if not clips: raise SystemExit(f"line {i + 1} has no clips")
        each = span / len(clips)
        for k, c in enumerate(clips):
            src = c["src"] if os.path.isabs(c["src"]) else os.path.join(DATA_ROOT, c["src"])
            p = os.path.join(tmp, f"seg{i:02d}_{k}.mp4"); segment(src, float(c.get("start", 0)), each, float(c.get("focus", 0.5)), p, kb=len(segs)); segs.append(p)
    lst = os.path.join(tmp, "segs.txt")
    with open(lst, "w") as f:
        for p in segs: f.write(f"file '{p}'\n")
    base = os.path.join(tmp, "base.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", base], check=True)
    # voice track
    vl = os.path.join(tmp, "voice.txt"); sil = os.path.join(tmp, "sil.wav")
    with wave.open(voice[0][0], "rb") as wf: vsr = wf.getframerate()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r={vsr}:cl=mono", "-t", f"{gap}", "-c:a", "pcm_s16le", sil], check=True)
    lead = os.path.join(tmp, "lead.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r={vsr}:cl=mono", "-t", "0.25", "-c:a", "pcm_s16le", lead], check=True)
    with open(vl, "w") as f:
        f.write(f"file '{lead}'\n")
        for _, _, vp, _ in timeline: f.write(f"file '{vp}'\nfile '{sil}'\n")
    narr = os.path.join(tmp, "narr.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", vl, "-ar", "44100", "-ac", "2", narr], check=True)
    # overlays (motion graphics): each = (png, x expr, y expr, start, end)
    st = os.path.join(tmp, "static.png"); static_png(spec.get("credit", ""), st)
    ov = [(st, "0", "0", 0, total)]
    flash = os.path.join(tmp, "flash.png"); solid_png(flash, (255, 255, 255, 150))
    labelled = [i for i, (_, _, _, l) in enumerate(timeline) if l.get("label")]
    title = spec.get("title_card")
    for i, (t0, vd, _, l) in enumerate(timeline):
        end = t0 + vd + gap
        if i > 0 and (l.get("label") or i == len(timeline) - 1) and l.get("label") != timeline[i - 1][3].get("label"):
            ov.append((flash, "0", "0", t0 - 0.03, t0 + 0.07))                       # white flash on each new point
        if i == 0 and title:                                                         # title card: badge drops in, lines slide in
            p = os.path.join(tmp, "badge.png"); bw, bh = badge_png(p)
            ov.append((p, str((W - bw) // 2), ease(-bh, 240, t0, 0.35), t0, end))
            top = text_img(title.get("top", "").upper(), 150, (255, 255, 255)); pt = os.path.join(tmp, "t_top.png"); top.save(pt)
            grad = {"green": ((90, 255, 120), (20, 200, 70)), "pink": ((255, 90, 200), (255, 170, 60)),
                    "red": ((255, 80, 80), (200, 20, 40))}.get(title.get("color", "green"))
            bot = text_img(title.get("bottom", "").upper(), 165, (90, 255, 120), gradient=grad); pb = os.path.join(tmp, "t_bot.png"); bot.save(pb)
            ov.append((pt, ease(-top.width, max(0, (W - top.width) // 2), t0 + 0.15), "760", t0 + 0.15, end))
            ov.append((pb, ease(W, max(0, (W - bot.width) // 2), t0 + 0.3), "950", t0 + 0.3, end))
        else:
            for k, (a, b, chunk) in enumerate(caption_chunks(words_of(l["say"]), t0, vd)):
                p = os.path.join(tmp, f"c{i:02d}_{k}.png"); w_, h_ = caption_png(chunk, p)
                pp = os.path.join(tmp, f"cp{i:02d}_{k}.png"); w2, h2 = caption_png(chunk, pp, 1.16)
                a = max(0, a)
                ov.append((pp, str((W - w2) // 2), str(CAP_Y - h2 // 2), a, a + 0.07))        # pop: big for 2 frames
                ov.append((p, str((W - w_) // 2), str(CAP_Y - h_ // 2), a + 0.07, b))
        if l.get("label"):
            kind = "never" if l["label"].upper().startswith("NEVER") else "num"
            p = os.path.join(tmp, f"l{i:02d}.png"); w_, h_ = label_pill(l["label"], p, kind)
            same = i > 0 and timeline[i - 1][3].get("label") == l["label"]
            ov.append((p, ("60" if same else ease(-w_, 60, t0)), "120", t0, end))
            if labelled:
                k = labelled.index(i) + 1 if i in labelled else 0
                pb = os.path.join(tmp, f"bar{i:02d}.png"); bar_png(pb, k, len(labelled)); ov.append((pb, "0", "40", t0, end))
        if l.get("emoji"):
            p = os.path.join(tmp, f"e{i:02d}.png"); sz = emoji_png(l["emoji"], p)
            if sz: ov.append((p, str(W - sz[0] - 70), ease(CAP_Y - 40, CAP_Y - 330, t0 + 0.1, 0.3), t0 + 0.1, end))
        if l.get("stamp"):
            p = os.path.join(tmp, f"s{i:02d}.png"); w_, h_ = stamp_png(l["stamp"].upper(), p)
            ts = t0 + vd * 0.55
            ov.append((p, f"'{(W - w_) // 2}+8*sin(t*70)*lt(t,{ts + 0.2:.2f})'", str(760 - h_ // 2), ts, end))
        if l.get("card"):
            p = os.path.join(tmp, f"k{i:02d}.png"); w_, h_ = card_png(l["card"], p)
            ov.append((p, str((W - w_) // 2), ease(H, 330, t0 + 0.05, 0.35), t0 + 0.05, end))
    music = sorted(x for x in os.listdir(MUSIC) if x.lower().endswith(".mp3")) if os.path.isdir(MUSIC) else []
    trk = os.path.join(MUSIC, random.Random(out).choice(music)) if music else None
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", base, "-i", narr]
    if trk: cmd += ["-stream_loop", "-1", "-i", trk]
    first = 3 if trk else 2
    for p, *_ in ov: cmd += ["-i", p]
    fc, last = [], "0:v"
    for k, (p, x, y, a, b) in enumerate(ov):
        nxt = f"o{k}"; fc.append(f"[{last}][{first + k}:v]overlay=x={x}:y={y}:eval=frame:enable='between(t,{a:.2f},{b:.2f})'[{nxt}]"); last = nxt
    fc.append(f"[{last}]format=yuv420p[v]")
    fc.append("[1:a]loudnorm=I=-15:TP=-1.5:LRA=7,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[vo][sc]")
    if trk:
        fc.append("[2:a]volume=0.16,aformat=sample_fmts=fltp:channel_layouts=stereo[m]")
        fc.append("[m][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[md]")
        fc.append("[vo][md]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        fc.append("[vo]anull[a]"); fc.append("[sc]anullsink")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0: raise SystemExit(r.stderr[-1500:])
    return {"out": out, "length_s": round(total, 1), "lines": len(lines), "music": os.path.basename(trk) if trk else None}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("spec"); ap.add_argument("out"); ap.add_argument("--handle", default=HANDLE)
    a = ap.parse_args(); HANDLE = "" if a.handle.lower() == "none" else a.handle
    print(json.dumps(render(json.load(open(a.spec, encoding="utf-8")), a.out)))
