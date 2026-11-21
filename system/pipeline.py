"""2-hour blocks -> moment detection -> candidate clips, plus raw cleanup and dashboard jobs.
Runs separately from the recorder: a slow cut never interrupts recording."""
import glob, json, os, re, statistics, time, datetime
from common import *
from recorder import LATENCY_S, add_gap

def coverage(sid, a, b, include_deleted=False):
    """Merged covered ranges of [a,b] from verified segments."""
    q = "SELECT wall_start s, wall_end e FROM segments WHERE stream_id=? AND verified=1 AND wall_end>? AND wall_start<?"
    if not include_deleted: q += " AND deleted=0"
    rs = sorted((max(a, r["s"]), min(b, r["e"])) for r in db_all(q, (sid, a, b)))
    out = []
    for s, e in rs:
        if out and s <= out[-1][1] + 2: out[-1][1] = max(out[-1][1], e)
        else: out.append([s, e])
    return out

def missing(sid, a, b, include_deleted=True):
    cov, gaps, cur = coverage(sid, a, b, include_deleted), [], a
    for s, e in cov:
        if s - cur > 2: gaps.append([cur, s])
        cur = max(cur, e)
    if b - cur > 2: gaps.append([cur, b])
    return gaps

def anchor(sid):
    st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    if st and st["broadcast_start"]: return st["broadcast_start"] + LATENCY_S
    r = db_one("SELECT MIN(wall_start) m FROM segments WHERE stream_id=?", (sid,))
    return r["m"] if r else None

def ensure_blocks(cfg, sid):
    a = anchor(sid); B = cfg["block_hours"] * 3600
    last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=? AND verified=1", (sid,))["m"]
    if a is None or last is None: return
    st = db_one("SELECT ended_at FROM streams WHERE id=?", (sid,))
    end_at = st["ended_at"] if st and st["ended_at"] else None
    stop = min(now(), end_at) if end_at else max(last, now())
    if end_at and whole_stream_block(cfg, sid, a, end_at): return
    for i in range(0, int((stop - a) // B) + 1):
        bs, be = a + i * B, a + (i + 1) * B
        if end_at:
            if bs >= end_at - 5: break                 # nothing after the end of the stream/video
            be = min(be, end_at)                       # the last block is shorter
        db_exec("INSERT OR IGNORE INTO blocks(id,stream_id,idx,start,end,status) VALUES(?,?,?,?,?,'recording')",
                (f"{sid}|{i}", sid, i, bs, be))
    if end_at:  # blocks made before the end time was known: trim / drop the ones past the end
        db_exec("UPDATE blocks SET end=? WHERE stream_id=? AND end>? AND start<?", (end_at, sid, end_at, end_at - 5))
        db_exec("DELETE FROM blocks WHERE stream_id=? AND start>=? AND status IN ('recording','missing','waiting_backfill','partial')", (sid, end_at - 5))

def whole_stream_block(cfg, sid, a, end_at):
    """Owner 2026-11-21: a finished stream that exists as ONE full file (his download, a full replay, an added video)
    is clipped as a whole, not in 2-hour blocks. Only live recording is still handled 2 hours at a time.
    Blocks already clipped keep their moments; the whole-stream block covers the rest (duplicates are skipped)."""
    if db_one("SELECT id FROM blocks WHERE id=?", (f"{sid}|w",)): return True
    full = db_one("SELECT * FROM segments WHERE stream_id=? AND verified=1 AND deleted=0 AND source!='live' "
                  "AND wall_start<=? AND wall_end>=? ORDER BY duration DESC LIMIT 1", (sid, a + 120, end_at - 120))
    if not full: return False
    if db_one("SELECT id FROM blocks WHERE stream_id=? AND status IN ('recording','waiting_backfill') AND id NOT LIKE '%|w' "
              "AND EXISTS (SELECT 1 FROM gaps g WHERE g.stream_id=blocks.stream_id AND g.status IN ('open','retry'))", (sid,)):
        return False
    db_exec("DELETE FROM blocks WHERE stream_id=? AND status!='clipped'", (sid,))
    db_exec("INSERT OR IGNORE INTO blocks(id,stream_id,idx,start,end,status) VALUES(?,?,?,?,?,'recording')",
            (f"{sid}|w", sid, 0, a, end_at))
    log(sid, "INFO", f"whole stream ({hms(end_at - a)}) will be clipped in one go, not in 2-hour blocks")
    return True

def evaluate_blocks(cfg, sid):
    st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    ended = st and (st["live_status"] in ("was_live", "post_live", "not_live") or (st["note"] or "") == "ended manually")
    live_edge = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=? AND source='live'", (sid,))["m"] or 0
    grace = cfg["block_grace_minutes"] * 60
    for b in db_all("SELECT * FROM blocks WHERE stream_id=? AND status IN ('recording','waiting_backfill')", (sid,)):
        if not (ended or live_edge >= b["end"] or now() > b["end"] + grace): continue
        cov = sum(e - s for s, e in coverage(sid, b["start"], b["end"], True)) / (b["end"] - b["start"])
        open_g = db_one("SELECT COUNT(*) n FROM gaps WHERE stream_id=? AND status IN ('open','retry') AND end>? AND start<?",
                        (sid, b["start"], b["end"]))["n"]
        if cov >= 0.995: status = "complete"
        elif open_g and (not ended or now() < b["end"] + grace): status = "waiting_backfill"
        elif cov < 0.02: status = "missing"
        else: status = "partial"
        if status != b["status"]:
            db_exec("UPDATE blocks SET status=?, coverage=? WHERE id=?", (status, cov, b["id"]))
            if status in ("partial", "missing"):
                miss = ", ".join(f"{hms(x-anchor(sid))}-{hms(y-anchor(sid))}" for x, y in missing(sid, b["start"], b["end"]))
                log(sid, "ALERT", f"block {b['idx']} ({hms(b['start']-anchor(sid))}-{hms(b['end']-anchor(sid))}) is {status}: missing {miss}")

def rms_series(cfg, seg, a=None, b=None):
    """Loudness per second of a segment -> (t0, [dB...]). Long files (a whole stream the owner dropped in, a full replay)
    are measured one block window at a time, so a 6-24 h file never runs into the time limit. An empty result is never
    cached: it is retried next time, and the reason (e.g. a download with no sound) is logged."""
    long = a is not None and (seg["duration"] or 0) > 3 * 3600
    off = max(0.0, a - seg["wall_start"]) if long else 0.0
    p = seg["path"] + (f".rms.{int(off)}.json" if long else ".rms.json")
    if os.path.exists(p):
        try:
            v = json.load(open(p))
            if v: return seg["wall_start"] + off, v
        except Exception: pass
    cmd = [cfg["ffmpeg"], "-hide_banner", "-nostats"] + (["-ss", f"{off:.2f}"] if long else []) + ["-i", seg["path"]]
    if long: cmd += ["-t", f"{b - a + 2:.2f}"]
    cmd += ["-map", "0:a:0?", "-vn", "-af",
            "aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f", "null", "-"]
    try: r = run(cmd, timeout=3600)
    except Exception as e:
        log(seg["stream_id"], "WARN", f"couldn't measure the sound of {os.path.basename(seg['path'])}: {type(e).__name__}: {e}"); return seg["wall_start"] + off, []
    vals = [float(x) if x not in ("-inf", "inf", "nan") else -90.0 for x in re.findall(r"RMS_level=(\S+)", r.stderr or "")]
    if not vals:
        tail = " ".join((r.stderr or "").strip().splitlines()[-3:])[:300]
        log(seg["stream_id"], "WARN", f"no sound levels from {os.path.basename(seg['path'])} (does the file have sound?): {tail}")
        return seg["wall_start"] + off, []
    try: os.remove(seg["path"] + ".rms.json") if long and os.path.getsize(seg["path"] + ".rms.json") <= 2 else None
    except OSError: pass
    json.dump(vals, open(p, "w"))
    return seg["wall_start"] + off, vals

def local_day(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")

def cut_source(cfg, sid, a, b, out):
    segs = db_all("SELECT * FROM segments WHERE stream_id=? AND verified=1 AND deleted=0 AND wall_end>? AND wall_start<? ORDER BY wall_start",
                  (sid, a, b))
    if not segs: return False
    # prefer a single segment that covers the whole window
    whole = [s for s in segs if s["wall_start"] <= a and s["wall_end"] >= b]
    if whole:
        s = whole[0]
        cmd = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a - s['wall_start']:.2f}", "-i", s["path"],
               "-t", f"{b - a:.2f}", "-c", "copy", "-movflags", "+faststart", out]
    else:
        chain, cur = [], a
        for s in segs:
            if s["wall_end"] > cur and (not chain or s["wall_start"] <= cur + 3):
                chain.append(s); cur = s["wall_end"]
        lst = out + ".txt"
        with open(lst, "w", encoding="utf-8") as f:
            for s in chain: f.write("file '" + s["path"].replace("'", "'\\''") + "'\n")
        cmd = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
               "-ss", f"{a - chain[0]['wall_start']:.2f}", "-t", f"{b - a:.2f}", "-c", "copy", "-movflags", "+faststart", out]
    r = run(cmd, timeout=600)
    if os.path.exists(out + ".txt"): os.remove(out + ".txt")
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 10000

def detect_block(cfg, b, force=False):
    sid, M = b["stream_id"], cfg["moment"]
    s = stream_cfg(cfg, sid)
    series = {}
    for seg in db_all("SELECT * FROM segments WHERE stream_id=? AND verified=1 AND deleted=0 AND wall_end>? AND wall_start<? ORDER BY wall_start",
                      (sid, b["start"], b["end"])):
        t0, vals = rms_series(cfg, seg, b["start"], b["end"])
        for i, v in enumerate(vals):
            t = int(t0) + i
            if b["start"] <= t < b["end"]: series.setdefault(t, v)
    if not series:
        # no sound levels (unreadable / silent file): don't mark it clipped with nothing, so the health check can retry it
        db_exec("UPDATE blocks SET status='clipped', clipped_at=?, n_moments=0 WHERE id=?", (now(), b["id"]))
        log(sid, "WARN", f"block {b['idx']}: no sound levels, 0 clips (the health check will retry it)"); return 0
    ts = sorted(series); vals = [series[t] for t in ts]
    scored = []
    for i, t in enumerate(ts):
        base = statistics.median(vals[max(0, i - 60):i + 60])
        scored.append((vals[i] - base, t))
    scored.sort(reverse=True)
    picks = []
    for sc, t in scored:
        if sc < M["threshold_db"] or len(picks) >= M["max_per_block"] * max(1.0, (b["end"] - b["start"]) / 7200): break
        if all(abs(t - p) > M["min_gap_s"] for _, p in picks): picks.append((sc, t))
    a0 = anchor(sid); n = 0
    for sc, t in sorted(picks, key=lambda x: x[1]):
        mid = f"{sid.split('@')[0]}_{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}"
        near = db_one("SELECT id FROM moments WHERE stream_id=? AND ABS(t_peak-?)<?", (sid, t, M["min_gap_s"]))
        if near and not force: continue
        d = os.path.join(CANDIDATES, sid.split("@")[0], local_day(t)); os.makedirs(d, exist_ok=True)
        a, e = max(b["start"], t - M["pre_s"]), min(b["end"], t + M["post_s"])
        src, sheet, meta = (os.path.join(d, mid + x) for x in (".mp4", ".jpg", ".json"))
        if not cut_source(cfg, sid, a, e, src): continue
        run([cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-vf",
             f"fps=8/{max(1,e-a):.0f},scale=360:-2,tile=4x2", "-frames:v", "1", sheet], timeout=120)
        info = {"id": mid, "stream_id": sid, "name": s["name"], "credit": s["credit"], "block": b["id"],
                "peak_time": iso(t), "broadcast_offset": hms(t - a0), "length_s": round(e - a, 1), "score_db": round(sc, 1),
                "source": os.path.relpath(src, DATA_ROOT).replace("\\", "/"), "sheet": os.path.relpath(sheet, DATA_ROOT).replace("\\", "/"), "day": local_day(t),
                "status": "candidate", "tag": None, "hook": None, "final": None, "posted": None, "created": iso(now())}
        json.dump(info, open(meta, "w", encoding="utf-8"), indent=1)
        db_exec("INSERT OR REPLACE INTO moments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mid, b["id"], sid, t, a, e, sc, src, sheet, meta, now()))
        n += 1
    db_exec("UPDATE blocks SET status='clipped', clipped_at=?, n_moments=n_moments+? WHERE id=?", (now(), n, b["id"]))
    log(sid, "INFO", f"block {b['idx']}: {n} candidate clips")
    return n

def cleanup(cfg):
    keep = cfg["raw_keep_hours_after_clip"] * 3600
    def deletable(seg, ignore_keep=False):
        bl = db_all("SELECT status, clipped_at, n_moments FROM blocks WHERE stream_id=? AND end>? AND start<?",
                    (seg["stream_id"], seg["wall_start"], seg["wall_end"]))
        # a block that gave 0 clips (no sound? a file still to be joined with its audio) keeps its footage a day
        wait = lambda x: keep if (x["n_moments"] or 0) > 0 or x["status"] == "missing" else max(keep, 24 * 3600)
        return bl and all(x["status"] in ("clipped", "missing") and (ignore_keep or (x["clipped_at"] or 0) + wait(x) < now()) for x in bl)
    def rm(seg):
        for p in [seg["path"], seg["path"] + ".rms.json"] + glob.glob(glob.escape(seg["path"]) + ".rms.*.json"):
            try: os.remove(p)
            except OSError: pass
        db_exec("UPDATE segments SET deleted=1 WHERE id=?", (seg["id"],))
    for seg in db_all("SELECT * FROM segments WHERE deleted=0 ORDER BY wall_start"):
        if deletable(seg): rm(seg)
    if free_gb() < cfg["min_free_gb"]:
        for seg in db_all("SELECT * FROM segments WHERE deleted=0 ORDER BY wall_start"):
            if free_gb() >= cfg["min_free_gb"]: break
            if deletable(seg, ignore_keep=True): rm(seg)
        flag = os.path.join(DATA, "pause_backfill_disk")
        if free_gb() < cfg["min_free_gb"]:
            if not os.path.exists(flag):
                open(flag, "w").close()
                log(None, "ALERT", f"disk almost full ({free_gb():.0f} GB free): backfill paused, live recording continues")
        elif os.path.exists(flag):
            os.remove(flag)

def add_video(cfg, p):
    """Add a finished YouTube video (re-upload, VOD, highlights) as its own timeline: it gets downloaded in
    30-minute pieces (newest first, ahead of older work), split into 2-hour blocks and clipped like a stream."""
    from recorder import fetch_info
    base = p["streamer"]; s = stream_cfg(cfg, base)
    info = fetch_info(cfg, p["url"])
    vid, dur = info.get("id"), float(info.get("duration") or 0)
    if not vid or dur < 30: return {"error": "couldn't read this video (is it public?)"}
    if info.get("live_status") == "is_live": return {"error": "this is a live stream: paste it on the Streams list instead"}
    sid = f"{base}@{vid}"
    if db_one("SELECT id FROM streams WHERE id=?", (sid,)): return {"error": "this video is already added", "id": sid}
    bstart = now() - dur - LATENCY_S - 60   # placed so its gap is the newest: it gets downloaded first
    db_exec("INSERT INTO streams(id,source_id,name,url,video_id,title,live_status,broadcast_start,ended_at,last_checked,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, base, s["name"], f"https://www.youtube.com/watch?v={vid}" if "youtu" in p["url"] else (info.get("webpage_url") or p["url"]), vid, info.get("title"), info.get("live_status") or "not_live",
             bstart, bstart + LATENCY_S + dur, now(), "added video"))
    db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created) VALUES(?,?,?,?,?,?)",
            (sid, bstart + LATENCY_S, bstart + LATENCY_S + dur, "open", "added video: to download", now()))
    log(sid, "INFO", f"added video '{info.get('title')}' ({hms(dur)}) for {s['name']}: downloading it now")
    return {"added": sid, "title": info.get("title"), "length": hms(dur)}

def end_stream(cfg, p):
    """The owner ends a timeline by hand (e.g. the streamer is asleep on stream): nothing after `end` is recorded,
    downloaded or clipped; missing footage before it is still filled."""
    sid = p["sid"]; st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    if not st: return {"error": "unknown stream"}
    end = now()
    if p.get("end"):
        end = datetime.datetime.fromisoformat(p["end"].replace("Z", "+00:00")).timestamp()
    db_exec("UPDATE streams SET ended_at=?, note='ended manually', recording=0 WHERE id=?", (end, sid))
    db_exec("DELETE FROM gaps WHERE stream_id=? AND start>=?", (sid, end - 5))
    db_exec("UPDATE gaps SET end=? WHERE stream_id=? AND end>? AND start<?", (end, sid, end, end))
    ensure_blocks(cfg, sid)
    log(sid, "ALERT", f"{st['name']}'s stream was ended at {iso(end)} by the owner: footage before that is still being completed and clipped")
    return {"ended": sid, "at": iso(end)}

def delete_timeline(cfg, p):
    """Remove a duplicate timeline (e.g. a partial upload fully covered by another timeline's full replay): its raw
    recordings, gaps, blocks and unedited/rejected cut moments. Finished or approved clips made from it are kept."""
    sid = p["sid"]; st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
    if not st: return {"error": "unknown timeline"}
    keep_by = p.get("covered_by")
    if keep_by and not db_one("SELECT id FROM streams WHERE id=?", (keep_by,)): return {"error": "covering timeline not found"}
    files = kept = 0
    for m in ([] if p.get("keep_moments") else db_all("SELECT * FROM moments WHERE stream_id=?", (sid,))):
        try: d = json.load(open(m["meta_path"], encoding="utf-8"))
        except Exception: d = {}
        if d.get("status") in ("final", "ready", "editing") or d.get("approved"): kept += 1; continue
        for f in (m["source_path"], m["sheet_path"], m["meta_path"]):
            try:
                if f and os.path.exists(f): os.remove(f); files += 1
            except Exception: pass
        db_exec("DELETE FROM moments WHERE id=?", (m["id"],))
    for seg in db_all("SELECT * FROM segments WHERE stream_id=?", (sid,)):
        for f in (seg["path"], seg["path"] + ".rms.json"):
            try:
                if os.path.exists(f): os.remove(f); files += 1
            except Exception: pass
    for t in ("segments", "gaps", "blocks"): db_exec(f"DELETE FROM {t} WHERE stream_id=?", (sid,))
    db_exec("DELETE FROM streams WHERE id=?", (sid,))
    try:
        import watcher; watcher.remember([st["video_id"]])     # never re-added by the channel watcher
    except Exception: pass
    log(None, "INFO", f"deleted duplicate timeline {sid} ({st['title']}): {files} files removed, {kept} finished/approved clips kept"
                      + (f"; it's covered by {keep_by}" if keep_by else ""))
    return {"deleted": sid, "files": files, "kept_clips": kept}

MODELS = {"small.en": "Systran/faster-whisper-small.en", "base.en": "Systran/faster-whisper-base.en"}

VOICES = {"en_US-ryan-high": "en/en_US/ryan/high", "en_US-lessac-high": "en/en_US/lessac/high",
          "en_US-joe-medium": "en/en_US/joe/medium", "en_US-bryce-medium": "en/en_US/bryce/medium",
          "en_US-john-medium": "en/en_US/john/medium", "en_US-norman-medium": "en/en_US/norman/medium",
          "en_US-hfc_male-medium": "en/en_US/hfc_male/medium", "en_US-kusal-medium": "en/en_US/kusal/medium",
          "en_US-ryan-medium": "en/en_US/ryan/medium", "en_GB-alan-medium": "en/en_GB/alan/medium"}

def fetch_url(url, dest):
    """Download a free model file (e.g. the Kokoro voice from its GitHub release) to a path under the data root."""
    import urllib.request
    dst = os.path.join(DATA_ROOT, dest); os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and os.path.getsize(dst) > 0: return {"ready": dest}
    log(None, "INFO", f"downloading {os.path.basename(dst)}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dst + ".part", "wb") as w:
        while True:
            b = r.read(1 << 20)
            if not b: break
            w.write(b)
    os.replace(dst + ".part", dst); return {"ready": dest}

def fetch_voice(name):
    """Piper AI voice (free, MIT/CC models from rhasspy/piper-voices) for the originals' narration."""
    import urllib.request
    out = os.path.join(DATA_ROOT, "tools", "voices"); os.makedirs(out, exist_ok=True)
    for ext in (".onnx.json", ".onnx"):
        dst = os.path.join(out, name + ext)
        if os.path.exists(dst) and os.path.getsize(dst) > 0: continue
        url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{VOICES[name]}/{name}{ext}"
        log(None, "INFO", f"downloading voice {name}{ext}")
        with urllib.request.urlopen(url, timeout=120) as r, open(dst + ".part", "wb") as w:
            while True:
                b = r.read(1 << 20)
                if not b: break
                w.write(b)
        os.replace(dst + ".part", dst)
    log(None, "INFO", f"voice {name} ready")
    return {"ready": name}

def fetch_model(p):
    if p.get("voice"): return fetch_voice(p["voice"])
    if p.get("url") and p.get("dest"): return fetch_url(p["url"], p["dest"])
    """Download a speech-to-text model (for subtitles) from Hugging Face into <data_root>/tools/models/<name>/."""
    import urllib.request
    name = p.get("name", "small.en"); repo = MODELS.get(name)
    if not repo: return {"error": "unknown model"}
    out = os.path.join(DATA_ROOT, "tools", "models", name); os.makedirs(out, exist_ok=True)
    for f in ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin"):
        dst = os.path.join(out, f)
        if os.path.exists(dst) and os.path.getsize(dst) > 0: continue
        url = f"https://huggingface.co/{repo}/resolve/main/{f}"
        log(None, "INFO", f"downloading {name}/{f}")
        with urllib.request.urlopen(url, timeout=120) as r, open(dst + ".part", "wb") as w:
            while True:
                b = r.read(1 << 20)
                if not b: break
                w.write(b)
        os.replace(dst + ".part", dst)
    log(None, "INFO", f"speech model {name} ready")
    return {"ready": out}

def run_job(cfg, j):
    p = json.loads(j["params"])
    if j["kind"] == "add_video": return add_video(cfg, p)
    if j["kind"] == "fetch_model": return fetch_model(p)
    if j["kind"] == "end_stream": return end_stream(cfg, p)
    if j["kind"] == "delete_timeline": return delete_timeline(cfg, p)
    if j["kind"] == "add_post":
        import posts; return posts.add(p, by=p.get("by", "claude"))
    sid = p["stream"]; a0 = anchor(sid)
    if a0 is None: return {"error": "no timeline for this stream yet"}
    if j["kind"] in ("cut", "capture"):
        a, b = a0 + p["start_s"], a0 + p["end_s"]
        miss = missing(sid, a, b, include_deleted=(j["kind"] == "capture"))
        if j["kind"] == "capture":
            for x, y in miss: add_gap(sid, x, y, "requested range")
            return {"queued_missing": [[hms(x - a0), hms(y - a0)] for x, y in miss], "already_have": not miss}
        if miss:
            return {"error": "not all of this range is on disk", "missing": [[hms(x - a0), hms(y - a0)] for x, y in miss],
                    "hint": "use 'capture' to fetch the missing parts (only if still available)"}
        out = os.path.join(MANUAL, f"{sid.split('@')[0]}_{hms(p['start_s']).replace(':','')}-{hms(p['end_s']).replace(':','')}.mp4")
        return {"file": os.path.relpath(out, DATA_ROOT).replace("\\", "/")} if cut_source(cfg, sid, a, b, out) else {"error": "cut failed"}
    if j["kind"] == "reprocess":
        b = db_one("SELECT * FROM blocks WHERE id=?", (p["block"],))
        if not b: return {"error": "unknown block"}
        if missing(sid, b["start"], b["end"], include_deleted=False) and not coverage(sid, b["start"], b["end"]):
            return {"error": "raw footage for this block has already been cleaned up"}
        return {"new_candidates": detect_block(cfg, b, force=bool(p.get("force")))}
    return {"error": "unknown job"}

def loop(cfg):
    last_clean = 0
    while True:
        try:
            for st in db_all("SELECT id FROM streams"):
                ensure_blocks(cfg, st["id"]); evaluate_blocks(cfg, st["id"])
            # jobs dropped as files by Claude's runs (data/job_inbox/*.json -> queued, file renamed .done)
            inbox = os.path.join(DATA, "job_inbox"); os.makedirs(inbox, exist_ok=True)
            for f in sorted(glob.glob(os.path.join(inbox, "*.json"))):
                try:
                    d = json.load(open(f, encoding="utf-8"))
                    if d.get("kind") in ("cut", "capture", "reprocess", "add_video", "add_post", "fetch_model", "end_stream", "delete_timeline"):
                        db_exec("INSERT INTO jobs(kind,params,status,created) VALUES(?,?,'queued',?)", (d["kind"], json.dumps(d.get("params", {})), now()))
                    os.replace(f, f[:-5] + ".done")
                except Exception as e:
                    log(None, "WARN", f"job file {os.path.basename(f)}: {e}"); os.replace(f, f[:-5] + ".bad")
            for j in db_all("SELECT * FROM jobs WHERE status='queued' ORDER BY id"):
                db_exec("UPDATE jobs SET status='running' WHERE id=?", (j["id"],))
                try: res = run_job(cfg, j)
                except Exception as e: res = {"error": str(e)}
                db_exec("UPDATE jobs SET status='done', result=?, done=? WHERE id=?", (json.dumps(res), now(), j["id"]))
            b = db_one("SELECT * FROM blocks WHERE status IN ('complete','partial') ORDER BY start LIMIT 1")
            if b: detect_block(cfg, b); continue
            if now() - last_clean > 600:
                cleanup(cfg); last_clean = now()
        except Exception as e:
            log(None, "ERROR", f"pipeline: {type(e).__name__}: {e}")
        time.sleep(20)
