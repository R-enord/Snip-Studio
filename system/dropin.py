"""Owner drop folder: C:\\Users\\abdul\\SnipRiot\\DROP_STREAMS_HERE\\<streamer>\\

Put each video in its streamer's folder (kai, dylan, speed, asianjeff, boeshi, bobby, ...). If the video has no sound,
put its sound file in the same folder with the same name (.m4a/.mp3...). Each video is paired with its sound and becomes
its own footage: it is registered as a new timeline of that streamer and clipped.
Nothing is matched to the server's streams, and nothing already on disk is overwritten. The file you dropped stays
where it is: the worker uses a hard link, so it takes no extra disk space."""
import glob, hashlib, json, os, time
from common import *

DROP = os.path.join(DATA_ROOT, "DROP_STREAMS_HERE")
PROCESSED = os.path.join(DATA, "drop_processed.json")
EXTS = (".mp4", ".mkv", ".webm", ".ts", ".mov", ".m4a", ".mp3", ".opus", ".aac", ".weba", ".ogg")
AUDIO_EXT = (".m4a", ".mp3", ".opus", ".aac", ".weba", ".ogg")
VIDEO_EXT = tuple(e for e in EXTS if e not in AUDIO_EXT)
WARNED = set()

def stable(p):
    try: s1 = os.path.getsize(p); m = os.path.getmtime(p)
    except OSError: return False
    if time.time() - m < 120: return False
    time.sleep(5)
    try: return os.path.getsize(p) == s1
    except OSError: return False

def load_done():
    try: return json.load(open(PROCESSED, encoding="utf-8"))
    except Exception: return {}

def save_done(d):
    tmp = PROCESSED + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, PROCESSED)

def warn_once(key, level, msg):
    if key in WARNED: return
    WARNED.add(key); log(None, level, msg)

def streamer_of(cfg, path):
    folder = os.path.relpath(os.path.dirname(path), DROP).split(os.sep)[0].lower()
    return folder if folder in {s["id"] for s in cfg.get("streams", [])} else None

def streams_of(cfg, path):
    """Which kinds of streams a file has: {'video', 'audio'}."""
    try:
        r = run([cfg["ffprobe"], "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path], timeout=60)
        return {x.strip().strip(",") for x in r.stdout.split() if x.strip()}
    except Exception: return set()

def find_audio(folder, stem):
    for f in os.listdir(folder):
        if os.path.splitext(f)[0] == stem and f.lower().endswith(AUDIO_EXT):
            return os.path.join(folder, f)
    return None

def handle(cfg, path, done):
    key = os.path.relpath(path, DROP).replace(os.sep, "/")
    streamer = streamer_of(cfg, path)
    name = os.path.basename(path); stem, ext = os.path.splitext(name); ext = ext.lower()
    dur = probe_duration(cfg, path) or 0
    if dur < 600:
        log(None, "ALERT", f"drop folder: {name} is too short or unreadable ({hms(dur)}): skipped")
        done[key] = {"status": "skipped", "why": "too short or unreadable"}; save_done(done); return
    kinds = streams_of(cfg, path)
    audio = find_audio(os.path.dirname(path), stem)
    if "audio" not in kinds and not audio:
        warn_once("nosound:" + key, "INFO", f"drop folder: {name} has no sound yet: put its sound file (same name, .m4a) in the same folder")
        return
    if audio and not stable(audio): return
    h = hashlib.sha1(f"{streamer}|{stem}".encode("utf-8")).hexdigest()[:10]
    vid = f"local_{h}"; sid = f"{streamer}@{vid}"
    base_dir = os.path.join(RAW, streamer, vid)
    dst = os.path.join(base_dir, f"full_{vid}_owner{ext}")
    if db_one("SELECT id FROM streams WHERE id=?", (sid,)) or os.path.exists(dst):
        log(sid, "ALERT", f"drop folder: {name} is already registered: not registered twice")
        done[key] = {"sid": sid, "status": "already registered"}; save_done(done); return
    os.makedirs(base_dir, exist_ok=True)
    try:
        os.link(path, dst)   # the drop-folder file stays where it is; the library name is a hard link
    except OSError as e:
        log(sid, "ALERT", f"drop folder: couldn't link {name} into the library ({e}): nothing changed"); return
    final = dst
    if "audio" not in kinds:
        out = os.path.join(base_dir, f"full_{vid}_owner_sound.mkv")
        r = run([cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", dst, "-i", audio,
                 "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", out], timeout=3 * 3600)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 0.9 * os.path.getsize(dst):
            log(sid, "ALERT", f"drop folder: couldn't join {os.path.basename(audio)} to {name}: {(r.stderr or '')[-200:]}")
            for f in (out, dst):   # only our own new names; the drop-folder originals are untouched
                if os.path.exists(f):
                    try: os.remove(f)
                    except OSError: pass
            return
        final = out
    display = next((s["name"] for s in cfg.get("streams", []) if s["id"] == streamer), streamer)
    t0 = now() - dur - 20
    db_exec("INSERT INTO streams(id,source_id,name,url,video_id,title,broadcast_start,ended_at,live_status,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, streamer, display, "", vid, stem, t0, t0 + 20 + dur, "ended", "dropped by owner"))
    st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    from backfill import use_replay
    use_replay(cfg, st, final, dur)
    done[key] = {"sid": sid, "status": "registered", "at": now(), "sound": os.path.basename(audio) if audio and final != dst else "in the video"}
    save_done(done)
    log(sid, "ALERT", f"drop folder: {display}: '{stem}' paired with its sound and registered ({hms(dur)}); it's being clipped now")

def loop(cfg):
    os.makedirs(DROP, exist_ok=True)
    while True:
        try:
            cfg = load_config()
            done = load_done()
            for p in sorted(glob.glob(os.path.join(DROP, "*", "*"))):
                if not os.path.isfile(p) or not p.lower().endswith(VIDEO_EXT): continue
                key = os.path.relpath(p, DROP).replace(os.sep, "/")
                if key in done or streamer_of(cfg, p) is None: continue
                if not stable(p): continue
                try: handle(cfg, p, done)
                except Exception as e: log(None, "WARN", f"drop folder: {os.path.basename(p)}: {e}")
            for p in glob.glob(os.path.join(DROP, "*")):
                if os.path.isfile(p) and p.lower().endswith(EXTS):
                    warn_once("root:" + p, "ALERT", f"drop folder: {os.path.basename(p)} is in the top folder: move it into its streamer's folder (kai, dylan, ...) with its sound file")
        except Exception as e:
            log(None, "WARN", f"drop folder loop: {e}")
        time.sleep(60)
