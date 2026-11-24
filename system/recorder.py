"""Live recorder: one worker per stream. Captures the live edge in 30-min segments,
registers each finished segment in the timeline, and opens gaps for anything missed
(earlier hours before we started, drops, PC off time). Backfill fills gaps separately."""
import csv, glob, json, os, re, time
from common import *

LATENCY_S = 20
PROCS = set()  # child processes, stopped when the worker restarts  # approx. capture delay behind the broadcast; offsets are +/- ~30s

PAUSE_RECORDING = os.path.join(DATA, "pause_recording")   # owner switch: no live recording while this file exists

def fetch_info(cfg, url):
    # skip the DASH listing (we record HLS): on very long live streams it can take minutes to fetch
    r = run(cfg["ytdlp_cmd"] + ["-J", "--no-warnings", "--no-playlist", "--socket-timeout", "30",
                              "--extractor-args", "youtube:skip=dash,translated_subs", url], timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    return json.loads(r.stdout)

UPCOMING = ("will begin in", "Premieres in", "This live event will begin", "is upcoming", "begin in a few moments")

TWITCH = re.compile(r"twitch\.tv/([A-Za-z0-9_]+)", re.I)
GTA6 = re.compile(r"GTA ?(6|VI)\b|GTA ?VI|Grand Theft Auto (6|VI)\b|Leonida|Vice City|\bGTA\b", re.I)

def twitch_login(url):
    """Channel login for a Twitch channel link (twitch.tv/lacy), None for anything else."""
    m = TWITCH.search(url or "")
    return m.group(1).lower() if m and m.group(1).lower() not in ("videos", "directory") else None

def twitch_vod(cfg, login, bstart=None):
    """The archive (VOD) of the channel's current/latest broadcast. Twitch keeps it growing while the stream is live,
    so gaps can be filled from it during the stream, like YouTube's rewind."""
    r = run(cfg["ytdlp_cmd"] + ["--flat-playlist", "-J", "--no-warnings", "--playlist-end", "1", "--socket-timeout", "30",
                              f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time"], timeout=120)
    if r.returncode != 0: return None
    e = (json.loads(r.stdout).get("entries") or [None])[0]
    if not e: return None
    ts = e.get("timestamp") or e.get("release_timestamp")
    if bstart and ts and abs(ts - bstart) > 3600: return None      # that archive is an older broadcast
    vid = str(e.get("id") or "").lstrip("v")
    return e.get("url") if "twitch.tv/videos" in (e.get("url") or "") else (f"https://www.twitch.tv/videos/{vid}" if vid else None)

def video_id(url):
    m = re.search(r"(?:v=|/live/|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None

def pick_hls(cfg, info):
    """Returns (video_format, audio_format_or_None). YouTube live now often offers separate
    video-only and audio-only HLS streams; older streams offer muxed ones."""
    H = cfg["max_height"]
    hls = [f for f in info.get("formats", []) if str(f.get("protocol", "")).startswith("m3u8")]
    has_v = lambda f: f.get("vcodec") not in (None, "none")
    has_a = lambda f: f.get("acodec") not in ("none",) and (f.get("acodec") is not None or not has_v(f))
    rank = lambda f: ((f.get("height") or 0), f.get("tbr") or 0)
    muxed = [f for f in hls if has_v(f) and f.get("acodec") not in (None, "none") and (f.get("height") or 0) <= H]
    if muxed:
        return max(muxed, key=rank), None
    vids = [f for f in hls if has_v(f) and (f.get("height") or 0) <= H]
    auds = [f for f in hls if not has_v(f) and has_a(f)]
    if vids and auds:
        return max(vids, key=rank), max(auds, key=lambda f: (f.get("abr") or f.get("tbr") or 0, str(f.get("format_id"))))
    return None, None

def add_gap(sid, start, end, note):
    if end - start <= 1:
        return
    for g in db_all("SELECT * FROM gaps WHERE stream_id=? AND status IN ('open','retry','unavailable') AND end>=? AND start<=?",
                    (sid, start - 2, end + 2)):
        if g["status"] == "unavailable" and g["start"] <= start + 5 and g["end"] >= end - 5:
            return  # already known to be lost
        if g["status"] != "unavailable":
            db_exec("UPDATE gaps SET start=?, end=? WHERE id=?", (min(g["start"], start), max(g["end"], end), g["id"]))
            return
    db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created) VALUES(?,?,?,?,?,?)",
            (sid, start, end, "open", note, now()))
    log(sid, "WARN", f"gap {iso(start)} -> {iso(end)} ({hms(end-start)}) {note}")

def subtract_from_gaps(sid, ws, we, tol=3):
    for g in db_all("SELECT * FROM gaps WHERE stream_id=? AND status!='filled' AND end>? AND start<?", (sid, ws + tol, we - tol)):
        left, right = ws - g["start"] > tol, g["end"] - we > tol
        if left and right:
            db_exec("UPDATE gaps SET end=? WHERE id=?", (ws, g["id"]))
            db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created,next_try) VALUES(?,?,?,?,?,?,?)",
                    (sid, we, g["end"], g["status"], g["note"], now(), g["next_try"]))
        elif left: db_exec("UPDATE gaps SET end=? WHERE id=?", (ws, g["id"]))
        elif right: db_exec("UPDATE gaps SET start=? WHERE id=?", (we, g["id"]))
        else: db_exec("UPDATE gaps SET status='filled' WHERE id=?", (g["id"],))

def repair_timeline(cfg):
    """Re-time live pieces run by run (back to back from the run's first piece), then rebuild each timeline's
    gap list from what is really on disk and drop blocks that were created in the future."""
    import re
    fixed = 0
    for st in db_all("SELECT * FROM streams"):
        sid = st["id"]
        segs = db_all("SELECT * FROM segments WHERE stream_id=? AND source='live' ORDER BY path", (sid,))
        runs = {}
        for g in segs:
            m = re.search(r"(live_\d{8}_\d{6})_(\d{5})\.ts$", g["path"])
            if m: runs.setdefault(m.group(1), []).append((int(m.group(2)), g))
        for run, items in runs.items():
            items.sort(); t = items[0][1]["wall_start"]
            for _, g in items:
                if abs(g["wall_start"] - t) > 2:
                    db_exec("UPDATE segments SET wall_start=?, wall_end=? WHERE id=?", (t, t + g["duration"], g["id"])); fixed += 1
                t = t + g["duration"]
        # rebuild gaps: holes between the stream start and the last saved footage
        a0 = (st["broadcast_start"] or 0) + LATENCY_S if st["broadcast_start"] else None
        rows = sorted((r["wall_start"], r["wall_end"]) for r in db_all("SELECT wall_start, wall_end FROM segments WHERE stream_id=? AND verified=1", (sid,)))
        if not rows or a0 is None: continue
        end = rows[-1][1] if not st["ended_at"] else max(rows[-1][1], st["ended_at"])
        holes, cur = [], a0
        for x, y in rows:
            if x - cur > 3: holes.append([cur, x])
            cur = max(cur, y)
        if end - cur > 3 and st["ended_at"]: holes.append([cur, end])
        lost = db_all("SELECT * FROM gaps WHERE stream_id=? AND status='unavailable'", (sid,))
        old = db_all("SELECT * FROM gaps WHERE stream_id=? AND status IN ('open','retry')", (sid,))
        note = old[0]["note"] if old else "recording interrupted"
        db_exec("DELETE FROM gaps WHERE stream_id=? AND status IN ('open','retry','unavailable')", (sid,))
        for x, y in holes:
            pieces = [[x, y]]
            for l in lost:  # keep what we already know is lost
                nxt = []
                for px, py in pieces:
                    if l["end"] <= px or l["start"] >= py: nxt.append([px, py]); continue
                    lx, ly = max(px, l["start"]), min(py, l["end"])
                    db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created) VALUES(?,?,?,'unavailable',?,?)", (sid, lx, ly, l["note"], now()))
                    if lx - px > 3: nxt.append([px, lx])
                    if py - ly > 3: nxt.append([ly, py])
                pieces = nxt
            for px, py in pieces:
                db_exec("INSERT INTO gaps(stream_id,start,end,status,note,created) VALUES(?,?,?,'open',?,?)", (sid, px, py, note, now()))
        db_exec("DELETE FROM blocks WHERE stream_id=? AND start>? AND status!='clipped'", (sid, now()))
        db_exec("UPDATE blocks SET status='recording' WHERE stream_id=? AND status IN ('missing','partial','complete','waiting_backfill')", (sid,))
    if fixed: log(None, "INFO", f"timeline repair: re-timed {fixed} recording piece(s) and rebuilt the gap list")

def recover_orphans(cfg):
    """After a restart or crash, register finished live pieces that were on disk but never logged."""
    import re, glob as _g
    seg = cfg["segment_minutes"] * 60; n = 0
    known = {r["path"] for r in db_all("SELECT path FROM segments")}
    for p in sorted(_g.glob(os.path.join(RAW, "*", "*", "live_*_*.ts"))):
        if p in known or time.time() - os.path.getmtime(p) < 5: continue   # runs at start-up, before any capture starts
        m = re.search(r"live_(\d{8})_(\d{6})_(\d{5})\.ts$", p)
        if not m: continue
        t_run = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")); k = int(m.group(3))
        start = t_run + 1 if k == 0 else (int(t_run // seg) + k) * seg  # pieces after the first start on clock boundaries
        base, vid = p.split(os.sep)[-3], p.split(os.sep)[-2]
        sid = f"{base}@{vid}"
        if not db_one("SELECT id FROM streams WHERE id=?", (sid,)): continue
        if register_segment(cfg, sid, p, start, start + 1, "live", aligned="approx"): n += 1
    if n: log(None, "INFO", f"recovered {n} recording piece(s) left over from a restart")

def register_segment(cfg, sid, path, ws, we, source, aligned="capture"):
    dur = probe_duration(cfg, path)
    if not dur or dur < 1:
        log(sid, "WARN", f"unreadable segment {os.path.basename(path)}")
        return None
    we = ws + dur  # trust the file's real duration
    seg_id = f"{sid}|{source}|{int(ws)}"
    if db_one("SELECT id FROM segments WHERE id=?", (seg_id,)):
        return seg_id
    prev = db_one("SELECT MAX(wall_end) e FROM segments WHERE stream_id=? AND verified=1 AND wall_start<?", (sid, ws))
    db_exec("INSERT INTO segments(id,stream_id,path,wall_start,wall_end,duration,source,aligned,verified,bytes) VALUES(?,?,?,?,?,?,?,?,1,?)",
            (seg_id, sid, path, ws, we, dur, source, aligned, os.path.getsize(path)))
    if source == "live":
        st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
        if prev and prev["e"]:
            if ws - prev["e"] > cfg["gap_tolerance_s"]:
                add_gap(sid, prev["e"], ws, "recording interrupted")
        elif st and st["broadcast_start"] and ws - (st["broadcast_start"] + LATENCY_S) > cfg["gap_tolerance_s"]:
            add_gap(sid, st["broadcast_start"] + LATENCY_S, ws, "before recording started")
        tot = db_one("SELECT SUM(bytes) b, SUM(duration) d FROM segments WHERE stream_id=? AND source='live'", (sid,))
        if tot["d"]:
            db_exec("UPDATE streams SET bytes_per_s=? WHERE id=?", (tot["b"] / tot["d"], sid))
    subtract_from_gaps(sid, ws, we)
    if source == "backfill":  # new footage for blocks already judged: look at them again (only new moments are added)
        db_exec("UPDATE blocks SET status='recording' WHERE stream_id=? AND end>? AND start<? AND status IN ('missing','partial','waiting_backfill','clipped')",
                (sid, ws, we))
    log(sid, "INFO", f"saved {source} segment {iso(ws)} {hms(dur)}")
    return seg_id

class Recorder:
    """One per streamer. Each broadcast (YouTube video id) gets its own timeline: '<id>@<video id>'.
    Re-reads config.json every cycle, so a new link for the next stream is picked up without a restart."""
    def __init__(self, cfg, s):
        self.cfg, self.base = cfg, s["id"]
        self.sid = None; self.proc = None

    def current(self):
        self.cfg = load_config()
        return next((x for x in self.cfg["streams"] if x["id"] == self.base), None)

    def loop(self):
        backoff = 0
        while True:
            s = self.current()
            if not s or not s.get("enabled", True):
                time.sleep(60); continue
            if os.path.exists(PAUSE_RECORDING):
                time.sleep(60); continue
            try:
                info = fetch_info(self.cfg, s["url"])
                login = twitch_login(s["url"])
                if login and info.get("description"):   # yt-dlp's Twitch "title" is just "<name> (live) <date>"; the real title is here
                    info["title"] = info["description"].strip()[:200]
                if login and info.get("description") and not GTA6.search(info.get("title") or ""):     # live on Twitch, but not playing GTA
                    if getattr(self, "_skip", None) != info.get("id"):
                        log(self.base, "INFO", f"{s['name']} is live on Twitch but not in a GTA stream ('{(info.get('title') or '')[:60]}'): not recording")
                        self._skip = info.get("id")
                    time.sleep(300); continue
                vid = info.get("id") or "unknown"
                self.sid = f"{self.base}@{vid}"
                self.dir = os.path.join(RAW, self.base, vid); os.makedirs(self.dir, exist_ok=True)
                db_exec("INSERT OR IGNORE INTO streams(id,source_id,name,url) VALUES(?,?,?,?)", (self.sid, self.base, s["name"], s["url"]))
                old = db_one("SELECT live_status FROM streams WHERE id=?", (self.sid,))["live_status"]
                status = info.get("live_status") or ("is_live" if info.get("is_live") else "unknown")
                bstart = (info.get("release_timestamp") or info.get("timestamp")) if status != "is_upcoming" else None
                rec_url = s["url"]
                if login and status == "is_live":   # a new Twitch broadcast id means the previous one has ended
                    for old_st in db_all("SELECT id FROM streams WHERE source_id=? AND live_status='is_live' AND id!=?", (self.base, self.sid)):
                        last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (old_st["id"],))["m"]
                        db_exec("UPDATE streams SET live_status='was_live', recording=0, ended_at=COALESCE(ended_at, ?) WHERE id=?", (last or now(), old_st["id"]))
                        log(old_st["id"], "INFO", "earlier Twitch broadcast has ended (a new one is live): filling its missing parts from the replay")
                if login:   # Twitch: the timeline's link is the broadcast's archive, which backfill can download from
                    row = db_one("SELECT url FROM streams WHERE id=?", (self.sid,))
                    rec_url = row["url"] if row and "twitch.tv/videos" in (row["url"] or "") else (twitch_vod(self.cfg, login, bstart) or s["url"])
                db_exec("UPDATE streams SET video_id=?, title=?, live_status=?, broadcast_start=COALESCE(?,broadcast_start), last_checked=?, url=? WHERE id=?",
                        (vid, info.get("title"), status, bstart, now(), rec_url, self.sid))
                if status != "is_live":
                    db_exec("UPDATE streams SET recording=0 WHERE id=?", (self.sid,))
                    if status in ("was_live", "post_live", "not_live") and old == "is_live":
                        st = db_one("SELECT * FROM streams WHERE id=?", (self.sid,))
                        length = hms(now() - st["broadcast_start"]) if st["broadcast_start"] else "?"
                        # real end time from the replay's length (the PC may have been off when the stream ended)
                        dur = float(info.get("duration") or 0)
                        end_t = st["broadcast_start"] + LATENCY_S + dur if (dur > 60 and st["broadcast_start"]) else now()
                        db_exec("UPDATE streams SET ended_at=? WHERE id=?", (min(end_t, now()), self.sid))
                        log(self.sid, "ALERT", f"{s['name']}'s stream has ended (ran about {length}). Send the link for the next stream. "
                                               "Any gaps will now be filled from the replay where YouTube keeps one.")
                    if status == "is_upcoming":
                        if old != "is_upcoming": log(self.sid, "INFO", f"{s['name']}'s next stream hasn't started yet: checking every minute, recording starts as soon as it goes live")
                        time.sleep(60); continue
                    for _ in range(10):  # wait ~10 min, but react quickly if a new link is added to config
                        time.sleep(60)
                        n = self.current()
                        if n and n["url"] != s["url"]: break
                    continue
                if old == "is_upcoming":
                    db_exec("UPDATE streams SET note=NULL WHERE id=?", (self.sid,))
                    log(self.sid, "ALERT", f"{s['name']} just went live: recording started")
                fmt, afmt = pick_hls(self.cfg, info)
                if not fmt:  # record what YouTube offered, for diagnosis, then let yt-dlp feed the stream itself
                    json.dump([[f.get("format_id"), f.get("protocol"), f.get("vcodec"), f.get("acodec"), f.get("height")]
                               for f in info.get("formats", [])], open(os.path.join(DATA, f"formats_{self.base}.json"), "w"))
                t_start = time.time()
                self.capture(fmt["url"] if fmt else None, fmt.get("height") if fmt else None, s["url"],
                             afmt["url"] if afmt else None)
                backoff = 0 if time.time() - t_start > 300 else min(backoff + 1, len(self.cfg["retry_backoff_s"]) - 1)
            except Exception as e:
                msg = str(e)
                if twitch_login(s["url"]) and ("not currently live" in msg or "is offline" in msg):
                    # Twitch channel offline: any of its broadcasts still marked live has ended
                    for st in db_all("SELECT * FROM streams WHERE source_id=? AND live_status='is_live'", (self.base,)):
                        last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (st["id"],))["m"]
                        db_exec("UPDATE streams SET live_status='was_live', recording=0, ended_at=COALESCE(ended_at, ?), last_checked=? WHERE id=?",
                                (last or now(), now(), st["id"]))
                        log(st["id"], "ALERT", f"{s['name']}'s Twitch stream has ended: missing parts will be filled from the replay")
                    time.sleep(60); continue
                if any(x in msg for x in UPCOMING):   # a scheduled stream that hasn't started: show it and keep checking
                    vid = video_id(s["url"]) or "unknown"
                    self.sid = f"{self.base}@{vid}"
                    db_exec("INSERT OR IGNORE INTO streams(id,source_id,name,url,video_id) VALUES(?,?,?,?,?)", (self.sid, self.base, s["name"], s["url"], vid))
                    old = db_one("SELECT live_status FROM streams WHERE id=?", (self.sid,))["live_status"]
                    m = re.search(r"begin in ([^.]+)", msg)
                    db_exec("UPDATE streams SET live_status='is_upcoming', recording=0, last_checked=?, note=? WHERE id=?",
                            (now(), ("starts in " + m.group(1).strip()) if m else "scheduled", self.sid))
                    if old != "is_upcoming": log(self.sid, "INFO", f"{s['name']}'s next stream hasn't started yet: checking every minute, recording starts as soon as it goes live")
                    time.sleep(60); continue
                if "Private video" in msg or "This video is private" in msg:   # streamer made the stream private (usually right after it ends)
                    vid = video_id(s["url"]) or "unknown"; self.sid = f"{self.base}@{vid}"
                    st = db_one("SELECT * FROM streams WHERE id=?", (self.sid,))
                    if st and (st["note"] or "") != "private":
                        last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (self.sid,))["m"]
                        db_exec("UPDATE streams SET live_status=CASE WHEN live_status='is_live' THEN 'was_live' ELSE live_status END, recording=0, note='private', "
                                "ended_at=COALESCE(ended_at, ?), last_checked=? WHERE id=?", (last or now(), now(), self.sid))
                        log(self.sid, "ALERT", f"{s['name']}'s stream was made private on YouTube, so recording stopped (the stream has most likely ended). "
                                               "Paste the link to the next stream; if this one comes back public, its missing parts get filled.")
                    for _ in range(10):  # check again in ~10 min, sooner if a new link is pasted
                        time.sleep(60)
                        n = self.current()
                        if n and n["url"] != s["url"]: break
                    continue
                net = any(x in msg for x in ("getaddrinfo", "Failed to resolve", "TransportError", "timed out", "Connection reset",
                                             "Network is unreachable", "10054", "10060", "10065"))
                log(self.sid or self.base, "ERROR", ("internet looks down: " if net else "") + f"{type(e).__name__}: {msg[:200]}")
                backoff = -1 if net else min(backoff + 1, len(self.cfg["retry_backoff_s"]) - 1)
            if self.sid: db_exec("UPDATE streams SET recording=0 WHERE id=?", (self.sid,))
            wait = 15 if backoff == -1 else self.cfg["retry_backoff_s"][backoff]
            if backoff == -1: backoff = 0
            log(self.sid or self.base, "INFO", f"reconnecting in {wait}s")
            time.sleep(wait)

    def close_final_gap(self):
        pass  # after the stream ends, open gaps stay queued for backfill from the replay

    def capture(self, hls_url, height, page_url=None, audio_url=None):
        run_id = time.strftime("%Y%m%d_%H%M%S")
        pattern = os.path.join(self.dir, f"live_{run_id}_%05d.ts")
        lst = os.path.join(self.dir, f"live_{run_id}.csv")
        cmd = [self.cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-rw_timeout", "30000000",
               "-i", hls_url, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
               "-f", "segment", "-segment_time", str(self.cfg["segment_minutes"] * 60), "-segment_atclocktime", "1",
               "-reset_timestamps", "1", "-segment_list", lst, "-segment_list_type", "csv", pattern]
        feeder = None
        if hls_url and audio_url:  # separate picture + sound: keep their shared clock so they stay in sync
            i = cmd.index("-i")
            cmd[i:i + 2] = ["-copyts", "-i", hls_url, "-rw_timeout", "30000000", "-i", audio_url]
            cmd[cmd.index("0:a:0")] = "1:a:0"
        if not hls_url:  # pipe mode: yt-dlp fetches the live stream (handles YouTube's tokens), ffmpeg segments it
            H = self.cfg["max_height"]
            feeder = popen(self.cfg["ytdlp_cmd"] + ["--no-warnings", "--quiet", "--no-part", "-f", f"b[height<={H}]/b",
                                                    "-o", "-", page_url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            PROCS.add(feeder)
            cmd[cmd.index(hls_url) if hls_url in cmd else cmd.index("-i") + 1] = "pipe:0"
            cmd.remove("-rw_timeout"); cmd.remove("30000000")
            log(self.sid, "INFO", "recording live edge (via yt-dlp)")
        else:
            log(self.sid, "INFO", f"recording live edge at {height}p" + (" (separate audio)" if audio_url else ""))
        self.proc = popen(cmd, stdin=feeder.stdout if feeder else None, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        PROCS.add(self.proc)
        import watchdog; watchdog.register(f"rec:{self.sid}", proc=self.proc, glob=pattern.replace("%05d", "*"), kind="rec", sid=self.sid)
        if feeder: feeder.stdout.close()
        anchor, s0, done, next_ws = None, None, set(), None
        db_exec("UPDATE streams SET recording=1, rec_since=? WHERE id=?", (now(), self.sid))
        while True:
            alive = self.proc.poll() is None
            if anchor is None:
                if glob.glob(pattern.replace("%05d", "*")):
                    anchor = time.time()
                    db_exec("UPDATE streams SET rec_since=? WHERE id=?", (anchor, self.sid))
            rows = []
            if os.path.exists(lst):
                with open(lst, newline="") as f:
                    rows = [r for r in csv.reader(f) if len(r) >= 3]
            for name, a, b in [r[:3] for r in rows]:
                if name in done or anchor is None:
                    continue
                # pieces of one run are back to back: each starts where the previous ended (the list's
                # timestamps can jump when picture and sound come from separate streams)
                ws = anchor if next_ws is None else next_ws
                sid_ = register_segment(self.cfg, self.sid, os.path.join(self.dir, name), ws, ws + 1, "live")
                seg = db_one("SELECT wall_end FROM segments WHERE id=?", (sid_,)) if sid_ else None
                if seg: next_ws = seg["wall_end"]
                done.add(name)
            if not alive:
                break
            if int(time.time()) % 60 < 10:   # the owner pasted a new link for this streamer: stop this capture and follow the new one
                cur = self.current()
                if cur and page_url and cur["url"] != page_url:
                    log(self.sid, "INFO", "a new link was added for this streamer: stopping this recording")
                    try: self.proc.kill()
                    except Exception: pass
            time.sleep(1 if anchor is None else 10)
        err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
        if feeder:
            try: feeder.kill()
            except Exception: pass
            PROCS.discard(feeder)
        PROCS.discard(self.proc)
        import watchdog; watchdog.done(f"rec:{self.sid}")
        # register any trailing file ffmpeg didn't list (crash / connection drop)
        for p in sorted(glob.glob(pattern.replace("%05d", "*"))):
            name = os.path.basename(p)
            if name not in done and anchor is not None:
                ws = anchor if next_ws is None else next_ws
                sid_ = register_segment(self.cfg, self.sid, p, ws, ws + 1, "live")
                seg = db_one("SELECT wall_end FROM segments WHERE id=?", (sid_,)) if sid_ else None
                if seg: next_ws = seg["wall_end"]
                done.add(name)
        log(self.sid, "WARN", f"capture ended (code {self.proc.returncode}) {err[-200:]}")
