"""Owner drop folder: %USERPROFILE%\\SnipRiot\\DROP_STREAMS_HERE
Put a whole stream video you downloaded yourself (mp4/mkv/webm/ts) in it. Once the file has stopped growing, it is matched
to its stream (by the YouTube/Twitch video id in the file name, else by its length), any server download of that stream
is stopped, and the file becomes that stream's footage (cut into 2-hour blocks and clipped like a replay).
Files that can't be matched go to DROP_STREAMS_HERE\\not matched, with an alert."""
import glob, os, re, shutil, time
from common import *

DROP = os.path.join(DATA_ROOT, "DROP_STREAMS_HERE")
NOMATCH = os.path.join(DROP, "not matched")
EXTS = (".mp4", ".mkv", ".webm", ".ts", ".mov", ".m4a", ".mp3", ".opus", ".aac", ".weba", ".ogg")

def stable(p):
    try: s1 = os.path.getsize(p); m = os.path.getmtime(p)
    except OSError: return False
    if time.time() - m < 120: return False
    time.sleep(5)
    try: return os.path.getsize(p) == s1
    except OSError: return False

def expected_len(st):
    if not st["broadcast_start"] or not st["ended_at"]: return None
    return st["ended_at"] - (st["broadcast_start"] + 20)

def match(cfg, path, dur):
    name = os.path.basename(path)
    ids = set(re.findall(r"[\[\(_ -]([A-Za-z0-9_-]{11})[\]\)_ .]", " " + name + " ")) | set(re.findall(r"v?(\d{9,11})", name))
    rows = db_all("SELECT * FROM streams")
    for st in rows:
        vid = (st["video_id"] or "").lstrip("v")
        if vid and (vid in ids or st["video_id"] in ids): return st, "video id in the file name"
        u = st["url"] or ""
        m = re.search(r"twitch\.tv/videos/(\d+)", u)
        if m and m.group(1) in ids: return st, "Twitch video id in the file name"
    # by length: finished streams whose length matches within 1% (and 3 minutes)
    cands = []
    for st in rows:
        L = expected_len(st)
        if L and abs(L - dur) <= max(180, 0.01 * L): cands.append((abs(L - dur), st))
    cands.sort(key=lambda x: x[0])
    if len(cands) == 1 or (len(cands) > 1 and cands[1][0] - cands[0][0] > 120): return cands[0][1], "length"
    return None, ("several streams have that length" if cands else "no stream with that length")

def stop_downloads(sid):
    import watchdog
    for key in (f"replay:{sid}", f"fill:{sid}"):
        a = watchdog.ACTIVE.get(key)
        if a and a.get("proc"):
            try: kill_tree(a["proc"])
            except Exception: pass
            a["killed"] = True
    time.sleep(5)
    base, vid = sid.split("@", 1)
    for f in glob.glob(os.path.join(RAW, base, vid, "full_*")) + glob.glob(os.path.join(RAW, base, vid, "bf_*.part*")):
        if ".part" in f or f.endswith(".ytdl"):
            try: os.remove(f)
            except Exception: pass

def streams_of(cfg, path):
    """Which kinds of streams a file has: {'video', 'audio'}."""
    try:
        r = run([cfg["ffprobe"], "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path], timeout=60)
        return {x.strip().strip(",") for x in r.stdout.split() if x.strip()}
    except Exception: return set()

def add_sound(cfg, path, st, dur):
    """An audio-only file for a stream whose dropped video has no sound: join them (no re-encoding) and clip again."""
    sid = st["id"]; base, vid = sid.split("@", 1)
    seg = db_one("SELECT * FROM segments WHERE stream_id=? AND deleted=0 AND path LIKE '%full_%' ORDER BY duration DESC LIMIT 1", (sid,))
    if not seg or not os.path.exists(seg["path"]):
        # the video may still be copying into the folder, or come later: keep the sound file waiting (up to 2 days)
        age = time.time() - os.path.getmtime(path)
        if age < 2 * 86400:
            if path not in WAITING:
                WAITING.add(path); log(sid, "INFO", f"drop folder: sound file for {st['name']} is waiting for its video")
            return
        log(sid, "ALERT", f"drop folder: a sound file for {st['name']} waited 2 days and no video of that stream came: moved to 'not matched'"); return park(path)
    if "audio" in streams_of(cfg, seg["path"]):
        log(sid, "ALERT", f"drop folder: {st['name']}'s video already has sound: the extra sound file was not needed"); return park(path)
    out = os.path.join(os.path.dirname(seg["path"]), f"full_{vid}_owner_sound.mkv")
    r = run([cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", seg["path"], "-i", path,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", out], timeout=3 * 3600)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < os.path.getsize(seg["path"]):
        log(sid, "ALERT", f"drop folder: couldn't join the sound file to {st['name']}'s video: {(r.stderr or '')[-200:]}"); return park(path)
    old = seg["path"]
    db_exec("UPDATE segments SET path=? WHERE id=?", (out, seg["id"]))
    for f in [old, path, old + ".rms.json"] + glob.glob(glob.escape(old) + ".rms.*.json"):
        try: os.remove(f)
        except OSError: pass
    db_exec("UPDATE blocks SET status='complete' WHERE stream_id=? AND status='clipped' AND n_moments=0", (sid,))
    log(sid, "ALERT", f"drop folder: added the sound to {st['name']}'s video ({hms(dur)}): it's being clipped now")

def handle(cfg, path):
    dur = probe_duration(cfg, path) or 0
    if dur < 600:
        log(None, "ALERT", f"drop folder: {os.path.basename(path)} is too short or unreadable ({hms(dur)}): skipped"); return park(path)
    kinds = streams_of(cfg, path)
    st, why = match(cfg, path, dur)
    if kinds and "video" not in kinds:   # a sound-only file (.m4a/.mp3/...) for a video dropped without sound
        if not st:
            log(None, "ALERT", f"drop folder: couldn't tell which stream the sound file {os.path.basename(path)} is for ({why})"); return park(path)
        return add_sound(cfg, path, st, dur)
    if not st:
        log(None, "ALERT", f"drop folder: couldn't tell which stream {os.path.basename(path)} ({hms(dur)}) is ({why}). "
                           "Put the video's link or id in the file name and drop it again."); return park(path)
    L = expected_len(st)
    if L and dur < 0.9 * L:
        log(st["id"], "ALERT", f"drop folder: {os.path.basename(path)} is {hms(dur)} but the stream ran {hms(L)}: is the download complete?"); return park(path)
    sid = st["id"]; base, vid = sid.split("@", 1)
    stop_downloads(sid)
    dst_dir = os.path.join(RAW, base, vid); os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"full_{vid}_owner" + os.path.splitext(path)[1].lower())
    shutil.move(path, dst)
    from backfill import use_replay
    st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    if not st["ended_at"]:
        db_exec("UPDATE streams SET ended_at=?, live_status=CASE WHEN live_status='is_live' THEN 'was_live' ELSE live_status END WHERE id=?",
                (st["broadcast_start"] + 20 + dur, sid)); st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    use_replay(cfg, st, dst, dur)
    log(sid, "ALERT", f"drop folder: using your download of '{st['title'] or vid}' ({hms(dur)}, matched by {why}); the server download was stopped")
    if kinds and "audio" not in kinds:
        log(sid, "ALERT", f"drop folder: your download of '{st['title'] or vid}' has NO SOUND, so no clips can be found or made from it. "
                          "Download the sound too (an .m4a/.mp3 audio-only file of the same video) and drop it in the folder: it gets joined automatically. "
                          "Or re-download the video with a format that has sound.")

WAITING = set()
AUDIO_EXT = (".m4a", ".mp3", ".opus", ".aac", ".weba", ".ogg")

def park(path):
    os.makedirs(NOMATCH, exist_ok=True)
    try: shutil.move(path, os.path.join(NOMATCH, os.path.basename(path)))
    except Exception: pass

def loop(cfg):
    os.makedirs(DROP, exist_ok=True)
    while True:
        cfg = load_config()
        files = glob.glob(os.path.join(DROP, "**", "*"), recursive=True)
        for p in sorted(files, key=lambda f: (f.lower().endswith(AUDIO_EXT), f)):   # videos first, then their sound files
            if NOMATCH in p or not p.lower().endswith(EXTS) or not os.path.isfile(p): continue
            if not stable(p): continue
            try: handle(cfg, p)
            except Exception as e: log(None, "WARN", f"drop folder: {os.path.basename(p)}: {e}")
        time.sleep(60)
