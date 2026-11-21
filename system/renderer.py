"""Renders clips on the PC itself, nonstop. Claude reviews each cut moment, writes its title/hook and sets the sidecar
"status": "ready"; this thread turns every ready moment into the finished house-style video (facecam on top, title box,
subtitles, music, watermark) and moves it to To review. It also re-renders finished clips made before the latest style
(old watermark / no subtitles), keeping their approval.
Python packages it needs (Pillow, faster-whisper) are installed once into <data_root>/tools/winlib."""
import glob, json, os, subprocess, sys, time
from common import *

WINLIB = os.path.join(DATA_ROOT, "tools", "winlib2")
PIPLOG = os.path.join(DATA, "logs", "renderer-install.log")
STYLE = os.path.join(SYSTEM_DIR, "style.py")
ORIG = os.path.join(SYSTEM_DIR, "originals.py")
STATE = os.path.join(DATA, "renderer.json")

def env():
    e = dict(os.environ); e["PYTHONPATH"] = WINLIB + os.pathsep + e.get("PYTHONPATH", ""); return e

def ready():
    r = subprocess.run([sys.executable, "-c", "import PIL, faster_whisper, piper"], env=env(), capture_output=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0

def install():
    lock = os.path.join(DATA, "renderer_install2.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 1800:
        time.sleep(60); return            # an install started before a restart may still be running
    open(lock, "w").write(iso(now()))
    os.makedirs(WINLIB, exist_ok=True)
    log(None, "INFO", "renderer: installing Pillow + faster-whisper for rendering on this PC (one time, a few minutes)")
    with open(PIPLOG, "a", encoding="utf-8") as lf:
        lf.write(f"\n=== {iso(now())} pip install ===\n"); lf.flush()
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--progress-bar", "off", "--only-binary=:all:",
                            "--target", WINLIB, "pillow", "faster-whisper", "piper-tts"],
                           stdout=lf, stderr=subprocess.STDOUT, text=True, timeout=3600, creationflags=CREATE_NO_WINDOW)
    r.stderr = r.stdout = open(PIPLOG, encoding="utf-8", errors="replace").read()[-400:]
    try: os.remove(lock)
    except Exception: pass
    if r.returncode != 0: raise RuntimeError((r.stderr or r.stdout)[-300:])
    log(None, "INFO", "renderer: ready")

_kokoro_tried = 0
def ensure_kokoro():
    """Kokoro: free, natural-sounding AI voice for the originals (Apache-2.0). One-time pip install into winlib2."""
    global _kokoro_tried
    r = subprocess.run([sys.executable, "-c", "import kokoro_onnx"], env=env(), capture_output=True, creationflags=CREATE_NO_WINDOW)
    if r.returncode == 0 or time.time() - _kokoro_tried < 3600: return
    _kokoro_tried = time.time()
    log(None, "INFO", "renderer: installing the Kokoro voice engine (one time)")
    with open(PIPLOG, "a", encoding="utf-8") as lf:
        lf.write(f"\n=== {iso(now())} pip install kokoro-onnx ===\n"); lf.flush()
        subprocess.run([sys.executable, "-m", "pip", "install", "--progress-bar", "off", "--target", WINLIB, "kokoro-onnx"],
                       stdout=lf, stderr=subprocess.STDOUT, text=True, timeout=3600, creationflags=CREATE_NO_WINDOW)

def save(p, d):
    tmp = p + ".tmp"; json.dump(d, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False); os.replace(tmp, p)

ACCOUNTS = {"a1": "@SnipRiotGTA", "a2": "@smartclips42"}

def mark_for(cfg, d, base=None):
    """Watermark for a clip: none for campaign streamers (their rules ban logos), else the account's handle."""
    if base:
        s = next((x for x in cfg.get("streams", []) if x.get("id") == base), {})
        if (s.get("campaign") or {}).get("no_watermark"): return "none"
    return ACCOUNTS.get(d.get("account") or "a1", "@SnipRiotGTA")

def have_mark(d):
    return d.get("wm_handle") or ("@SnipRiotGTA" if d.get("wm") == 2 else None)

def todo(cfg):
    subs = bool(cfg.get("subtitles"))
    fresh, upgrades = [], []
    for p in glob.glob(os.path.join(CANDIDATES, "*", "*", "*.json")):
        try: d = json.load(open(p, encoding="utf-8"))
        except Exception: continue
        if d.get("status") == "ready" and d.get("title") and (d.get("render_fails") or 0) < 3:
            fresh.append((d.get("created") or "", p, d))
        elif d.get("status") == "final" and d.get("title") and not d.get("posted") and (d.get("render_fails") or 0) < 3 \
                and (have_mark(d) != mark_for(cfg, d, (d.get("stream_id") or "").split("@")[0]) or (subs and not d.get("subs"))):
            upgrades.append((d.get("created") or "", p, d))
    for p in glob.glob(os.path.join(ORIGINALS, "*", "*.json")):
        if p.endswith(".spec.json"): continue
        try: d = json.load(open(p, encoding="utf-8"))
        except Exception: continue
        if d.get("status") == "ready" and (d.get("render_fails") or 0) < 3 and os.path.exists(p[:-5] + ".spec.json"):
            fresh.append(("0", p, d))          # originals first: only 2 a day and they're time-sensitive
    fresh.sort(key=lambda x: x[0]); upgrades.sort(key=lambda x: x[0])
    return fresh + upgrades

def render_original(cfg, p, d):
    spec, out = p[:-5] + ".spec.json", p[:-5] + ".mp4"
    t0 = time.time()
    mark = mark_for(cfg, d)
    r = subprocess.run([sys.executable, ORIG, spec, out + ".part.mp4", "--handle", mark], env=env(), capture_output=True, text=True, timeout=3600,
                       creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
    d = json.load(open(p, encoding="utf-8"))
    if r.returncode != 0 or not os.path.exists(out + ".part.mp4"):
        d["render_fails"] = (d.get("render_fails") or 0) + 1; d["render_error"] = (r.stderr or r.stdout)[-300:]
        save(p, d); log(None, "WARN", f"renderer: original {os.path.basename(p)} failed: {d['render_error'][-150:]}"); return False
    os.replace(out + ".part.mp4", out)
    d.update(status="final", final=os.path.relpath(out, DATA_ROOT).replace("\\", "/"), wm=2, wm_handle=mark, rendered=iso(now()), rendered_by="pc", kind="original")
    d.pop("render_error", None); d.pop("render_fails", None); save(p, d)
    log(None, "INFO", f"renderer: original {os.path.basename(out)} done in {int(time.time() - t0)}s")
    return True

def render(cfg, p, d):
    base = d["stream_id"].split("@")[0]
    cams = {}
    try: cams = json.load(open(os.path.join(DATA, "cams.json"), encoding="utf-8"))
    except Exception: pass
    cam = (cams.get(base) or {}).get("cam")
    out = os.path.join(LIBRARY, d["day"], base, d["id"] + ".mp4"); os.makedirs(os.path.dirname(out), exist_ok=True)
    subs = bool(cfg.get("subtitles"))
    cmd = [sys.executable, STYLE, os.path.join(DATA_ROOT, d["source"]), out + ".part.mp4", "--title", d["title"],
           "--credit", d.get("name") or base]
    if cam: cmd += ["--cam", ",".join(str(x) for x in cam)]
    if subs: cmd += ["--subs"]
    mark = mark_for(cfg, d, base); cmd += ["--handle", mark]
    t0 = time.time()
    r = subprocess.run(cmd, env=env(), capture_output=True, text=True, timeout=1800, creationflags=CREATE_NO_WINDOW,
                       encoding="utf-8", errors="replace")
    d = json.load(open(p, encoding="utf-8"))   # re-read: the owner may have changed it meanwhile
    if r.returncode != 0 or not os.path.exists(out + ".part.mp4"):
        d["render_fails"] = (d.get("render_fails") or 0) + 1; d["render_error"] = (r.stderr or r.stdout)[-300:]
        save(p, d); log(None, "WARN", f"renderer: {d['id']} failed: {d['render_error'][-150:]}"); return False
    os.replace(out + ".part.mp4", out)
    if d.get("status") in ("ready", "final"):
        d.update(status="final", final=os.path.relpath(out, DATA_ROOT).replace("\\", "/"), broll=True, wm=2, wm_handle=mark, subs=subs,
                 rendered=iso(now()), rendered_by="pc")
        d.pop("render_error", None); d.pop("render_fails", None)
        save(p, d)
    log(None, "INFO", f"renderer: {d['id']} done in {int(time.time() - t0)}s")
    return True

def loop(cfg):
    while True:
        try:
            cfg = load_config()
            if not ready():
                install()
                if not ready(): continue
            try: ensure_kokoro()
            except Exception as e: log(None, "WARN", f"renderer: Kokoro install failed: {e}")
            if not os.path.exists(os.path.join(DATA_ROOT, "tools", "voices", "en_US-ryan-high.onnx")):
                os.makedirs(os.path.join(DATA, "job_inbox"), exist_ok=True)
                f = os.path.join(DATA, "job_inbox", "fetch_voice.json")
                if not os.path.exists(f): json.dump({"kind": "fetch_model", "params": {"voice": "en_US-ryan-high"}}, open(f, "w"))
            jobs = todo(cfg)
            json.dump({"t": iso(now()), "queue": len(jobs), "ok": True}, open(STATE, "w"))
            if not jobs: time.sleep(60); continue
            _, p, d = jobs[0]
            if p.startswith(ORIGINALS): render_original(cfg, p, d)
            else: render(cfg, p, d)
        except Exception as e:
            log(None, "ERROR", f"renderer: {type(e).__name__}: {str(e)[:200]}")
            try: json.dump({"t": iso(now()), "ok": False, "error": str(e)[:200]}, open(STATE, "w"))
            except Exception: pass
            time.sleep(300)
