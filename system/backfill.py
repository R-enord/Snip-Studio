"""Backfill: fills timeline gaps (earlier hours, PC-off time, drops) from YouTube's rewind
window while live, or from the replay after the stream ends. Never downloads a range we
already have: it only ever works on open gaps, in chunks, oldest first."""
import datetime, glob, os, time
from common import *
from recorder import register_segment, LATENCY_S, fetch_info, PROCS

PAUSE_FLAG = os.path.join(DATA, "pause_backfill")        # manual pause (Claude/owner)
DISK_FLAG = os.path.join(DATA, "pause_backfill_disk")    # set by the disk guard

def mark_unavailable(g, note):
    db_exec("UPDATE gaps SET status='unavailable', note=? WHERE id=?", (note, g["id"]))
    log(g["stream_id"], "ALERT", f"footage {iso(g['start'])} -> {iso(g['end'])} ({hms(g['end']-g['start'])}) can't be recovered: {note}")

def chunk_range(g, cfg):
    """Newest-first: take the chunk at the END of the gap, so the most recent 2-hour blocks complete first."""
    ce = g["end"]; cs = max(g["start"], ce - cfg["backfill"]["chunk_minutes"] * 60)
    return cs, ce

def isoz(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def try_chunk_live(cfg, g, st):
    """While live: fetch only the missing minutes from YouTube's rewind buffer with ypb."""
    sid = g["stream_id"]; bf = cfg["backfill"]; pad = 10
    cs, ce = chunk_range(g, cfg)
    b0, b1 = cs - LATENCY_S - pad, ce - LATENCY_S + pad            # broadcast-time moments
    out_dir = os.path.join(RAW, *sid.split("@")); os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"bf_{int(cs)}")
    H = cfg["max_height"]
    cmd = cfg["ypb_cmd"] + ["download", "--host", "127.0.0.1", "--port", str(bf.get("ypb_port", 9123)), "--no-metadata",
           "-i", f"{isoz(b0)}/{isoz(b1)}", st["video_id"], "--",
           "-f", f"bv*[height<={H}][vcodec^=avc1]+ba/bv*[height<={H}]+ba/b", "--merge-output-format", "mkv", "--no-part",
           "-o", stem + ".%(ext)s"]
    if bf.get("rate_limit"): cmd += ["-r", str(bf["rate_limit"])]
    log(sid, "INFO", f"refetching {hms(ce-cs)} from YouTube's rewind ({iso(cs)})")
    expect = (b1 - b0) * (st["bytes_per_s"] or 1_000_000); limit = expect * 2.5 + 50e6
    proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE); PROCS.add(proc)
    import watchdog; watchdog.register(f"fill:{sid}", proc=proc, glob=stem + "*", kind="fill", sid=sid, expected=int(expect), label="rewind")
    t0 = time.time(); killed = None
    while proc.poll() is None:
        size = sum(os.path.getsize(p) for p in glob.glob(stem + "*") if os.path.isfile(p))
        if size > limit: killed = "download much larger than the requested range"
        elif time.time() - t0 > (b1 - b0) * 4 + 900: killed = "timed out"
        if killed: proc.kill(); break
        time.sleep(5)
    PROCS.discard(proc); watchdog.done(f"fill:{sid}")
    err = (proc.stderr.read() or b"").decode(errors="replace")
    files = [p for p in glob.glob(stem + ".*") if p.endswith((".mkv", ".mp4", ".webm"))]
    if killed or proc.returncode not in (0, None) or not files:
        for p in glob.glob(stem + "*"):
            try: os.remove(p)
            except OSError: pass
        return f"failed: {killed or err.strip()[-300:] or 'no file'}"
    path = files[0]; dur = probe_duration(cfg, path) or 0
    if dur < 0.5 * (ce - cs):
        return f"failed: got {hms(dur)} of {hms(ce-cs)}"
    seg_start = b0 + LATENCY_S
    register_segment(cfg, sid, path, seg_start, seg_start + dur, "backfill", aligned="approx")
    if not st["earliest_available"] or seg_start < st["earliest_available"]:
        db_exec("UPDATE streams SET earliest_available=? WHERE id=?", (seg_start, sid))
    return "ok"

def try_chunk(cfg, g, st):
    sid = g["stream_id"]; bf = cfg["backfill"]
    cs, ce = chunk_range(g, cfg)
    bstart = st["broadcast_start"]
    p0, p1 = cs - bstart - LATENCY_S - 5, ce - bstart - LATENCY_S + 5   # small pad so keyframe snapping leaves no slivers
    if p1 <= 0:  # the whole remaining gap is before the broadcast started
        db_exec("UPDATE gaps SET status='filled' WHERE id=?", (g["id"],)); return "skip"
    p0 = max(0, p0)
    out_dir = os.path.join(RAW, *sid.split("@")); os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"bf_{int(cs)}")
    H = cfg["max_height"]
    cmd = cfg["ytdlp_cmd"] + ["--no-warnings", "--no-playlist", "--no-part",
           "-f", f"bv*[height<={H}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={H}]+ba/b[height<={H}]",
           "--download-sections", f"*{hms(p0)}-{hms(p1)}", "--merge-output-format", "mkv",
           "-o", stem + ".%(ext)s", st["url"]]
    if st["live_status"] == "is_live" and "twitch.tv" not in (st["url"] or ""):
        cmd.insert(len(cfg["ytdlp_cmd"]), "--live-from-start")
    expect = (p1 - p0) * (st["bytes_per_s"] or 1_000_000)
    limit = expect * 2.5 + 50e6
    log(sid, "INFO", f"backfill {hms(p0)}-{hms(p1)} of broadcast")
    proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE); PROCS.add(proc)
    import watchdog; watchdog.register(f"fill:{sid}", proc=proc, glob=stem + "*", kind="fill", sid=sid, expected=int(expect), label="replay part")
    t0 = time.time(); killed = None
    while proc.poll() is None:
        size = sum(os.path.getsize(p) for p in glob.glob(stem + "*") if os.path.isfile(p))
        if size > limit:
            killed = "download larger than the requested range (range download not supported here)"
        elif time.time() - t0 > (p1 - p0) * 2 + 900:
            killed = "timed out"
        if killed:
            proc.kill(); break
        time.sleep(5)
    PROCS.discard(proc); watchdog.done(f"fill:{sid}")
    err = (proc.stderr.read() or b"").decode(errors="replace")
    files = [p for p in glob.glob(stem + ".*") if p.endswith((".mkv", ".mp4", ".webm", ".ts"))]
    if killed or proc.returncode not in (0, None) or not files:
        for p in glob.glob(stem + "*"):
            try: os.remove(p)
            except OSError: pass
        return f"failed: {killed or err.strip()[-300:] or 'no file'}"
    path = files[0]
    dur = probe_duration(cfg, path) or 0
    if dur < 0.8 * (p1 - p0):
        return f"failed: got {hms(dur)} of {hms(p1-p0)} (outside the rewind window?)"
    seg_start = bstart + p0 + LATENCY_S
    register_segment(cfg, sid, path, seg_start, seg_start + dur, "backfill", aligned="approx")
    if not st["earliest_available"] or seg_start < st["earliest_available"]:
        db_exec("UPDATE streams SET earliest_available=? WHERE id=?", (seg_start, sid))
    return "ok"   # register_segment() has already trimmed/closed the gap

def missing_s(sid):
    r = db_one("SELECT SUM(end-start) m FROM gaps WHERE stream_id=? AND status IN ('open','retry')", (sid,))
    return (r["m"] or 0) if r else 0

def want_full(cfg, st):
    """After a stream ends: download the whole replay in one go (and use it as the stream's footage) when a big part
    is missing; small gaps are still fetched on their own so we don't re-download hours we already have."""
    if (st["note"] or "") in ("full replay", "private", "ended manually"): return False
    if st["live_status"] in ("is_live", "post_live", "is_upcoming"): return False
    total = (st["ended_at"] or now()) - (st["broadcast_start"] + LATENCY_S)
    if total <= 0: return False
    if "twitch.tv/videos" in (st["url"] or "") and total < 48 * 3600:
        return True   # Twitch live pieces contain ad breaks ("Commercial break in progress"); the archive has none
    # pick the faster way: the whole replay in one download when a big share is missing or the holes are many and
    # scattered (lots of small downloads); otherwise only the missing parts (less to download)
    share = float(cfg["backfill"].get("full_replay_if_missing", 0.4))
    holes = db_one("SELECT COUNT(*) n FROM gaps WHERE stream_id=? AND status IN ('open','retry') AND end-start>60", (st["id"],))["n"]
    return missing_s(st["id"]) >= share * total or holes >= int(cfg["backfill"].get("full_replay_if_holes", 6))

def full_replay(cfg, st):
    sid = st["id"]; bf = cfg["backfill"]; H = cfg["max_height"]
    out_dir = os.path.join(RAW, *sid.split("@")); os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"full_{st['video_id']}")
    cmd = cfg["ytdlp_cmd"] + ["--no-warnings", "--no-playlist", "--continue",
           "-f", f"bv*[height<={H}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={H}]+ba/b[height<={H}]",
           "--merge-output-format", "mkv", "-o", stem + ".%(ext)s", st["url"]]
    if bf.get("rate_limit"): cmd += ["-r", str(bf["rate_limit"])]
    total = (st["ended_at"] or now()) - (st["broadcast_start"] + LATENCY_S)
    busy = [x for x in glob.glob(stem + "*") if time.time() - os.path.getmtime(x) < 90]
    if busy:  # a download left running by the previous worker (before a restart) is still writing: let it finish
        time.sleep(60); return "paused"
    log(sid, "INFO", f"stream is over and {hms(missing_s(sid))} is missing: downloading the full replay ({hms(total)}) instead of piece by piece")
    proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE); PROCS.add(proc)
    import watchdog
    bps = st["bytes_per_s"] if "bytes_per_s" in st.keys() and st["bytes_per_s"] else 0
    watchdog.register(f"replay:{sid}", proc=proc, glob=stem + "*", kind="replay", sid=sid, expected=int(bps * total) if bps else None)
    t0 = time.time()
    while proc.poll() is None:
        if time.time() - t0 > total * 4 + 7200: proc.kill(); PROCS.discard(proc); return "failed: full replay download timed out"
        if os.path.exists(PAUSE_FLAG) or os.path.exists(DISK_FLAG): proc.kill(); return "paused"
        if sid in skipped(load_config()): kill_tree(proc); log(sid, "INFO", "stopped the replay download: you're downloading this one yourself"); return "paused"
        time.sleep(10)
    PROCS.discard(proc)
    a = watchdog.ACTIVE.get(f"replay:{sid}") or {}; watchdog.done(f"replay:{sid}")
    if a.get("killed"): return "paused"          # restarted by the watchdog: try again (it resumes)
    err = (proc.stderr.read() or b"").decode(errors="replace")
    files = [x for x in glob.glob(stem + ".*") if x.endswith((".mkv", ".mp4", ".webm")) and ".part" not in x and ".f" not in os.path.basename(x)[len(os.path.basename(stem)):]]
    if proc.returncode != 0 or not files:
        return f"failed: {err.strip()[-300:] or 'no file'}"
    path = files[0]; dur = probe_duration(cfg, path) or 0
    if dur < 0.9 * total:
        return f"failed: replay is only {hms(dur)} of {hms(total)}"
    return use_replay(cfg, st, path, dur)

def use_replay(cfg, st, path, dur):
    """Make `path` (the whole stream) this timeline's footage: register it, drop the live/partial pieces, close gaps."""
    sid = st["id"]
    ws = st["broadcast_start"] + LATENCY_S
    seg_id = register_segment(cfg, sid, path, ws, ws + dur, "backfill", aligned="replay")
    # the replay replaces the pieces recorded live: drop them (cut moments already made from them are kept)
    freed = 0
    for seg in db_all("SELECT * FROM segments WHERE stream_id=? AND id!=? AND deleted=0", (sid, seg_id)):
        for f in (seg["path"], seg["path"] + ".rms.json"):
            try:
                if os.path.exists(f): freed += os.path.getsize(f); os.remove(f)
            except Exception: pass
        db_exec("UPDATE segments SET deleted=1 WHERE id=?", (seg["id"],))
    if freed: log(sid, "INFO", f"replaced the live-recorded pieces with the replay ({freed / 1e9:.1f} GB freed)")
    db_exec("UPDATE gaps SET status='filled', note='covered by the full replay' WHERE stream_id=? AND status IN ('open','retry') AND start>=? AND end<=?",
            (sid, ws - 5, ws + dur + 5))
    db_exec("UPDATE streams SET note='full replay' WHERE id=?", (sid,))
    log(sid, "INFO", f"full replay saved ({hms(dur)}): the whole stream is covered and gets cut into 2-hour blocks (the last one shorter)")
    return "ok"

WAIT_NOTE = "waiting for the replay (YouTube won't give a time range while the stream is live)"

def recheck_s(cfg):
    return max(1, float(cfg["backfill"].get("recheck_minutes", 30))) * 60

def defer(g, cfg):
    db_exec("UPDATE gaps SET status='retry', next_try=?, note=? WHERE id=?", (now() + recheck_s(cfg), WAIT_NOTE, g["id"]))

def migrate():
    # earlier versions wrongly gave up on footage that is only unavailable *while live*
    n = db_exec("UPDATE gaps SET status='open', attempts=0, next_try=0, note=? WHERE status='unavailable' AND note LIKE '%cannot be partially downloaded%'",
                (WAIT_NOTE,)).rowcount
    db_exec("UPDATE gaps SET next_try=0 WHERE status IN ('open','retry') AND note=?", (WAIT_NOTE,))
    db_exec("UPDATE blocks SET status='recording' WHERE status IN ('missing','partial') AND stream_id IN (SELECT id FROM streams WHERE live_status='is_live')")
    if n: log(None, "INFO", f"re-opened {n} gaps that were wrongly marked lost; they'll be filled from the replay")
    m = db_exec("UPDATE gaps SET status='open', attempts=0, next_try=0 WHERE status='unavailable' AND note LIKE '%rivate video%'").rowcount
    db_exec("UPDATE streams SET last_checked=0 WHERE note='private'")
    db_exec("UPDATE streams SET note=NULL WHERE note='full replay' AND id NOT IN (SELECT stream_id FROM segments WHERE path LIKE '%full_%')")   # re-check private videos right away
    if m: log(None, "INFO", f"re-opened {m} gaps that failed because a video was private")

def skipped(cfg):
    """Streams the owner is downloading himself (Settings backfill.skip_streams): the server doesn't download them."""
    return set((cfg.get("backfill") or {}).get("skip_streams") or [])

def total_s(st):
    if not st or not st["broadcast_start"]: return 0
    return max(1.0, (st["ended_at"] or now()) - (st["broadcast_start"] + LATENCY_S))

def started_s(st):
    """Seconds of a full replay already part-downloaded (counts as progress)."""
    try:
        b = sum(os.path.getsize(f) for f in glob.glob(os.path.join(RAW, *st["id"].split("@"), "full_*")) if os.path.isfile(f))
        bps = st["bytes_per_s"] if "bytes_per_s" in st.keys() and st["bytes_per_s"] else 0
        return b / bps if bps else 0
    except Exception: return 0

def fill_order(cfg, gaps=None):
    """Owner's rule: whatever is closest to done goes first. Order of streams for the gap-fill lane:
    1) finished streams with under 10 minutes left (quick wins), 2) live streams (the rewind buffer expires),
    3) other finished streams by the share still missing, smallest first. Returns [(sid, [gaps])]."""
    skip = skipped(cfg)
    if gaps is None:
        gaps = [g for g in db_all("SELECT * FROM gaps WHERE status IN ('open','retry') AND next_try<=?", (now(),)) if g["stream_id"] not in skip]
    by = {}
    for g in gaps: by.setdefault(g["stream_id"], []).append(g)
    rows = {r["id"]: r for r in db_all("SELECT * FROM streams")}
    def key(sid):   # owner: sort purely by how much is already captured (highest % first); tiny leftovers first
        st = rows.get(sid); left = sum(g["end"] - g["start"] for g in by[sid])
        if left <= 600: return (0, left)
        return (1, max(0.0, left - started_s(st)) / total_s(st) if st else 9)
    return [(sid, by[sid]) for sid in sorted(by, key=key)]

def next_gap(cfg):
    order = fill_order(cfg)
    if not order: return None
    sid, gs = order[0]
    return max(gs, key=lambda g: g["end"])

def loop(cfg):
    migrate()
    while True:
        try:
            cfg = load_config()  # settings changed in the control centre apply on the next cycle
            if not cfg["backfill"]["enabled"] or os.path.exists(PAUSE_FLAG) or os.path.exists(DISK_FLAG):
                time.sleep(60); continue
            g = next_gap(cfg)
            if not g:
                time.sleep(30); continue
            if g["end"] - g["start"] <= 3:   # a sliver of a couple of seconds left by keyframe snapping
                db_exec("UPDATE gaps SET status='filled' WHERE id=?", (g["id"],)); continue
            st = db_one("SELECT * FROM streams WHERE id=?", (g["stream_id"],))
            # broadcasts the recorder no longer follows (the streamer moved to a new link): check their status here,
            # so a finished replay gets used for backfill
            if st and "twitch.tv" not in (st["url"] or "") and (st["live_status"] in ("is_live", "post_live") or (st["note"] or "") == "private") and now() - (st["last_checked"] or 0) > min(600, recheck_s(cfg)):
                try:
                    info = fetch_info(cfg, st["url"])
                    stat = info.get("live_status") or st["live_status"]
                    dur = float(info.get("duration") or 0)
                    end_t = min(now(), st["broadcast_start"] + LATENCY_S + dur) if (dur > 60 and st["broadcast_start"] and stat != "is_live") else now()
                    db_exec("UPDATE streams SET live_status=?, last_checked=?, ended_at=COALESCE(ended_at, CASE WHEN ?!='is_live' THEN ? END) WHERE id=?",
                            (stat, now(), stat, end_t, st["id"]))
                    if stat != st["live_status"]: log(st["id"], "INFO", f"broadcast status now {stat}")
                    if (st["note"] or "") == "private":
                        db_exec("UPDATE streams SET note=NULL WHERE id=?", (st["id"],))
                        db_exec("UPDATE gaps SET status='open', attempts=0, next_try=0 WHERE stream_id=? AND status IN ('unavailable','retry') AND (note LIKE '%rivate%' OR note=?)",
                                (st["id"], WAIT_NOTE))
                        log(st["id"], "ALERT", f"{st['name']}'s video '{st['title'] or st['video_id']}' is public again: downloading its missing parts")
                    st = db_one("SELECT * FROM streams WHERE id=?", (g["stream_id"],))
                except Exception as e:
                    msg = str(e)
                    if "Private video" in msg and (st["note"] or "") != "private":
                        db_exec("UPDATE streams SET note='private' WHERE id=?", (st["id"],))
                        log(st["id"], "ALERT", f"{st['name']}'s video '{st['title'] or st['video_id']}' was made private, so its missing parts can't be downloaded. "
                                               "They stay waiting: if it comes back, or you add the re-upload (Streams → Add a finished video), they'll be filled.")
                    log(st["id"], "WARN", f"couldn't check broadcast status: {msg[:150]}")
                    db_exec("UPDATE streams SET last_checked=? WHERE id=?", (now(), st["id"]))
            if not st or not st["broadcast_start"]:
                mark_unavailable(g, "broadcast start time unknown, so earlier footage can't be located"); continue
            twitch_vod = "twitch.tv/videos" in (st["url"] or "")   # Twitch archive: downloadable while still live
            live_ypb = st["live_status"] == "is_live" and not twitch_vod and "twitch.tv" not in (st["url"] or "") and cfg.get("ypb_cmd") and os.path.exists(cfg["ypb_cmd"][0])
            if live_ypb:
                res = try_chunk_live(cfg, g, st)          # rewind buffer, while live
                if res == "ok": continue
            elif st["live_status"] in ("is_live", "post_live") and not twitch_vod:  # no rewind tool: wait for the finished replay
                defer(g, cfg); continue
            elif want_full(cfg, st):     # the replay lane downloads the whole stream; don't fetch it piece by piece
                db_exec("UPDATE gaps SET next_try=? WHERE stream_id=? AND status IN ('open','retry')", (now() + 900, st["id"])); continue
            else:
                res = try_chunk(cfg, g, st)
            if "cannot be partially downloaded" in res:
                defer(g, cfg); continue
            if res.startswith("failed") and "Private video" in res:
                db_exec("UPDATE streams SET note='private' WHERE id=?", (st["id"],))
                db_exec("UPDATE gaps SET status='retry', next_try=?, note=? WHERE id=?", (now() + recheck_s(cfg), "video is private on YouTube", g["id"]))
                continue
            if res.startswith("failed"):
                n = g["attempts"] + 1
                old = False  # replays and the rewind buffer don't expire quickly: always retry before giving up
                if n >= cfg["backfill"]["max_attempts"] or old:
                    cs, ce = chunk_range(g, cfg)
                    if cs - g["start"] > 2:   # keep the older part of the gap open, give up on this chunk only
                        db_exec("UPDATE gaps SET end=?, attempts=0, next_try=0 WHERE id=?", (cs, g["id"]))
                        db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created) VALUES(?,?,?,?,?,?)",
                                (g["stream_id"], cs, ce, "unavailable", res[:300], now()))
                        log(g["stream_id"], "ALERT", f"footage {iso(cs)} -> {iso(ce)} ({hms(ce-cs)}) can't be recovered: {res[:200]}")
                    else:
                        mark_unavailable(g, res)
                else:
                    db_exec("UPDATE gaps SET status='retry', attempts=?, next_try=?, note=? WHERE id=?",
                            (n, now() + cfg["backfill"].get("retry_s", 300) * n, res[:300], g["id"]))
        except Exception as e:
            log(None, "ERROR", f"backfill: {type(e).__name__}: {e}")
            time.sleep(60)


def queues(cfg):
    """Who's waiting for what, for the control centre: {sid: {"fill": position, "replay": position}}."""
    q = {}
    for i, (sid, _) in enumerate(fill_order(cfg)): q.setdefault(sid, {})["fill"] = i + 1
    skip = skipped(cfg)
    todo = [st for st in db_all("SELECT * FROM streams") if st["broadcast_start"] and st["id"] not in skip and want_full(cfg, st)]
    todo.sort(key=lambda x: max(0.0, missing_s(x["id"]) - started_s(x)) / total_s(x))
    for i, st in enumerate(todo): q.setdefault(st["id"], {})["replay"] = i + 1
    return q

def replay_loop(cfg):
    """Second lane: full-replay downloads of finished streams run alongside the live rewind fills (which can't wait)."""
    time.sleep(30)
    while True:
        try:
            cfg = load_config()
            if os.path.exists(PAUSE_FLAG) or os.path.exists(DISK_FLAG): time.sleep(120); continue
            skip = skipped(cfg)
            todo = [st for st in db_all("SELECT * FROM streams ORDER BY broadcast_start") if st["broadcast_start"] and st["id"] not in skip and want_full(cfg, st)]
            if not todo: time.sleep(120); continue
            # closest to done first: share of the stream still to download (what's already part-downloaded counts)
            todo.sort(key=lambda x: max(0.0, missing_s(x["id"]) - started_s(x)) / total_s(x))
            st = todo[0]
            res = full_replay(cfg, st)
            if res in ("ok", "paused"): continue
            if any(x in res for x in ("getaddrinfo", "Failed to resolve", "timed out", "Connection", "10054", "10060")):
                log(st["id"], "WARN", "internet looks down: will retry the full replay download in a minute"); time.sleep(60); continue
            log(st["id"], "WARN", f"full replay download didn't work ({res[:150]}): fetching the missing parts one by one instead")
            db_exec("UPDATE streams SET note='full replay' WHERE id=? AND (note IS NULL OR note!='private')", (st["id"],))
            db_exec("UPDATE gaps SET next_try=0 WHERE stream_id=? AND status IN ('open','retry')", (st["id"],))
        except Exception as e:
            log(None, "ERROR", f"replay lane: {type(e).__name__}: {e}"); time.sleep(120)
