"""SnipRiot original-video renderer (tips, how-tos, hidden details, news recaps, Day X recaps).
Same house look as style.py: title in a white rounded box, step cards in the same box style,
full-height gameplay b-roll from our own recordings (or official Rockstar media), music bed.

Usage: python explainer.py SPEC.json OUT.mp4
SPEC = {
  "title": "Best way to get money fast in GTA 6 💰",
  "clips": [{"src": "<path>", "start": 12.0, "end": 18.5, "game": [x,y,w,h]}],   # game box = fractions of the source frame (optional)
  "cards": [{"text": "1. Rob the gas station", "start": 0, "end": 4}],          # seconds on the final timeline
  "credit": "Footage: IShowSpeed · Kai Cenat",
  "keep_game_audio": 0.35, "music": "auto"
}"""
import json, os, subprocess, sys, tempfile
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import style
from style import W, H, TEXT_FONT, measure, wrap, draw_rich, pick_music, crop_expr

def card_png(text, path, px=52):
    font = ImageFont.truetype(TEXT_FONT, px)
    lines = wrap(text, font, px, 900)
    lh = int(px * 1.25)
    im = Image.new("RGBA", (W, lh * len(lines) + 40), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        tw = measure(ln, font, px); x0 = (W - tw) / 2; y0 = 10 + i * lh
        d.rounded_rectangle((x0 - 22, y0 - 6, x0 + tw + 22, y0 + lh), radius=16, fill=(255, 255, 255, 240))
    for i, ln in enumerate(lines):
        tw = measure(ln, font, px); draw_rich(im, ((W - tw) / 2, 10 + i * lh - px * 0.06), ln, font, px, (0, 0, 0, 255))
    im.save(path)

def credit_png(text, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = ImageFont.truetype(TEXT_FONT, 28); tw = measure(text, f, 28)
    d.rounded_rectangle((28, H - 400, 28 + tw + 28, H - 356), radius=22, fill=(0, 0, 0, 150))
    draw_rich(im, (42, H - 396), text, f, 28, (255, 255, 255, 255))
    fh = ImageFont.truetype(TEXT_FONT, 26)
    style.watermark(im)
    im.save(path)

def probe(src):
    o = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                   "-of", "json", src], capture_output=True, text=True).stdout)
    return o["streams"][0]["width"], o["streams"][0]["height"]

def render(spec, out):
    tmp = tempfile.mkdtemp(); parts = []
    for i, c in enumerate(spec["clips"]):  # each b-roll piece -> 1080x1920, 30fps, stereo 48k
        sw, sh = probe(c["src"]); game = c.get("game") or [0.21875, 0, 0.5625, 1]
        p = os.path.join(tmp, f"p{i}.mp4"); parts.append(p)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(c["start"]), "-t", str(c["end"] - c["start"]),
                        "-i", c["src"], "-vf", crop_expr(game, sw, sh, W, H) + ",fps=30,format=yuv420p,setsar=1",
                        "-af", "aresample=48000,aformat=channel_layouts=stereo", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                        "-c:a", "aac", "-b:a", "160k", p], check=True)
    lst = os.path.join(tmp, "l.txt"); open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    body = os.path.join(tmp, "body.mp4")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", body], check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", body],
                               capture_output=True, text=True).stdout)
    tp = os.path.join(tmp, "t.png"); style.title_png(spec["title"], tp)
    cp = os.path.join(tmp, "c.png"); credit_png(spec.get("credit", ""), cp)
    inputs = ["-i", body, "-i", tp, "-i", cp]; v = "[0:v][1:v]overlay=0:170[a0];[a0][2:v]overlay=0:0[b0]"; last = "b0"
    for k, card in enumerate(spec.get("cards", [])):
        pth = os.path.join(tmp, f"k{k}.png"); card_png(card["text"], pth); inputs += ["-i", pth]; n = 3 + k
        v += f";[{last}][{n}:v]overlay=0:1180:enable='between(t,{card['start']},{card['end']})'[c{k}]"; last = f"c{k}"
    v += f";[{last}]format=yuv420p[v]"
    trk = pick_music(out) if spec.get("music", "auto") == "auto" else spec.get("music")
    g = spec.get("keep_game_audio", 0.35); fade = f"afade=t=in:d=0.3,afade=t=out:st={max(0, dur - 0.8):.2f}:d=0.8"
    if trk:
        m = len(inputs) // 2
        inputs += ["-stream_loop", "-1", "-i", trk]
        a = f"[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,volume={g}[ga];[{m}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.45[mu];[ga][mu]amix=inputs=2:normalize=0:duration=first,{fade}[a]"
    else:
        a = f"[0:a]volume={g},{fade}[a]"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs + ["-filter_complex", v + ";" + a,
                    "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out], check=True)
    return {"out": out, "length_s": round(dur, 1), "music": os.path.basename(trk) if trk else None}

if __name__ == "__main__":
    print(render(json.load(open(sys.argv[1], encoding="utf-8")), sys.argv[2]))
