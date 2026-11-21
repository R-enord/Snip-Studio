"""Watchdog: every minute it measures every running recording and replay download (bytes on disk, speed, time left)
for the control centre, and restarts anything stuck: a live recording whose file hasn't grown for 3 minutes, or a
replay download that hasn't grown for 10 minutes. Results: data/downloads.json."""
import glob, json, os, time
from common import *

ACTIVE = {}      # key -> {"proc", "glob", "kind": "rec"|"replay", "sid", "expected"}
OUT = os.path.join(DATA, "downloads.json")
LIMIT = {"rec": 180, "replay": 600, "fill": 600}

def register(key, **kv):
    kv.setdefault("t0", time.time()); ACTIVE[key] = kv

def done(key): ACTIVE.pop(key, None)

def size(pattern):
    tot = 0
    for f in glob.glob(pattern):
        try: tot += os.path.getsize(f)
        except OSError: pass
    return tot

HEALTH = os.path.join(DATA, "health.json")
_said = {}

def health_check(cfg):
    """Every 5 minutes: look over every stream and download and write data/health.json for the Overview page.
    Anything found is also logged as an alert (once an hour per problem)."""
    issues = []
    streams = db_all("SELECT * FROM streams")
    enabled = {s["id"] for s in cfg.get("streams", []) if s.get("enabled", True)}
    if not cfg.get("streams"):
        issues.append(("settings", "no streamers are set up in Settings > Streams: nothing new gets recorded or clipped"))
    try:   # the same error over and over = a stuck thread (2026-11-21: pipeline failed every 20 s for 10 hours)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        tail = open(os.path.join(DATA, "logs", f"{day}.log"), encoding="utf-8", errors="replace").read()[-60000:].splitlines()
        cut = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now() - 900))
        errs = [l for l in tail if "[ERROR]" in l and l[:16] >= cut]
        if len(errs) >= 10:
            what = errs[-1].split("] ", 1)[-1][:160]
            issues.append(("errors", f"{len(errs)} errors in the last 15 min, latest: {what}"))
    except Exception: pass
    for st in streams:
        base = st["source_id"] or st["id"].split("@")[0]
        if base not in enabled: continue
        if st["live_status"] == "is_live" and not st["recording"] and now() - (st["last_checked"] or 0) < 900:
            last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (st["id"],))["m"] or 0
            if now() - last > 600: issues.append((st["id"], f"{st['name']} is live but nothing has been recorded for {int((now() - last) // 60)} min"))
    by = {}
    for st in streams:
        if st["live_status"] == "is_live": by.setdefault(st["source_id"], []).append(st)
    for base, rows in by.items():
        if len(rows) > 1:
            # two broadcasts of one streamer marked live: keep the one being recorded (else the newest), the rest have ended
            rows.sort(key=lambda r: (1 if r["recording"] else 0, r["broadcast_start"] or 0))
            for old in rows[:-1]:
                last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (old["id"],))["m"]
                if old["recording"]: continue
                db_exec("UPDATE streams SET live_status='was_live', ended_at=COALESCE(ended_at, ?) WHERE id=?", (last or now(), old["id"]))
                log(old["id"], "INFO", "health check: an older broadcast was still marked live while a newer one is running: marked it ended (missing parts will be filled)")
    try: auto_finish(cfg, streams, issues)
    except Exception as e: log(None, "ERROR", f"health check (auto finish): {type(e).__name__}: {e}")
    try:
        r = json.load(open(os.path.join(DATA, "renderer.json"), encoding="utf-8"))
        import datetime
        age = now() - datetime.datetime.fromisoformat(r["t"].replace("Z", "+00:00")).timestamp()
        if age > 900: issues.append(("renderer", f"the clip renderer hasn't reported for {int(age // 60)} min"))
    except Exception: pass
    try:
        import shutil
        free = shutil.disk_usage(DATA_ROOT).free / 1e9
        if free < float(cfg.get("min_free_gb", 20)) * 1.5: issues.append(("disk", f"only {free:.0f} GB free on the laptop"))
    except Exception: pass
    for sid, msg in issues:
        if time.time() - _said.get(msg, 0) > 3600: log(None if sid in ("renderer", "disk") else sid, "ALERT", "health check: " + msg); _said[msg] = time.time()
    tmp = HEALTH + ".tmp"; json.dump({"t": iso(now()), "issues": [{"what": sid, "msg": m} for sid, m in issues]}, open(tmp, "w")); os.replace(tmp, HEALTH)

RECLIP = os.path.join(DATA, "reclip_tries.json")

def auto_finish(cfg, streams, issues):
    """Owner: things must change by themselves. 1) A finished stream whose last few seconds/minutes never download
    (YouTube keeps refusing them) gets those marked lost, so the stream counts as done and moves to History.
    2) Blocks that were 'clipped' with 0 clips because no sound levels could be read (e.g. right after a dropped-in file)
    are sent back to clipping, up to 3 times, 1 h apart; after that it's reported."""
    for st in streams:   # a broadcast still marked live long after a newer broadcast of the same streamer started: it ended
        if st["ended_at"] or st["live_status"] != "is_live" or not st["broadcast_start"]: continue
        last = db_one("SELECT MAX(wall_end) m FROM segments WHERE stream_id=?", (st["id"],))["m"] or st["broadcast_start"]
        newer = [x for x in streams if x["id"] != st["id"] and (x["source_id"] or x["id"].split("@")[0]) == (st["source_id"] or st["id"].split("@")[0])
                 and (x["broadcast_start"] or 0) > last]
        if newer and now() - last > 2 * 3600:
            db_exec("UPDATE streams SET live_status='was_live', recording=0, ended_at=? WHERE id=?", (last, st["id"]))
            db_exec("DELETE FROM gaps WHERE stream_id=? AND start>=?", (st["id"], last - 5))
            log(st["id"], "INFO", f"health check: {st['name']}'s broadcast from {iso(st['broadcast_start'])} was still marked live although a newer one started: marked it ended at {iso(last)}")
    for st in streams:
        if not st["ended_at"] or now() - st["ended_at"] < 3 * 3600 or not st["broadcast_start"]: continue
        gs = db_all("SELECT * FROM gaps WHERE stream_id=? AND status IN ('open','retry')", (st["id"],))
        if not gs: continue
        left = sum(g["end"] - g["start"] for g in gs); L = max(1.0, st["ended_at"] - st["broadcast_start"])
        tried = all((g["attempts"] or 0) >= 2 for g in gs) or now() - st["ended_at"] > 12 * 3600
        if left <= max(300.0, 0.01 * L) and tried:
            db_exec("UPDATE gaps SET status='unavailable', note=COALESCE(note,'')||' | gave up: YouTube never gave these few seconds' "
                    "WHERE stream_id=? AND status IN ('open','retry')", (st["id"],))
            log(st["id"], "INFO", f"health check: {st['name']}: the last {hms(left)} never downloaded after several tries: marked lost, the stream is finished")
    try: tries = json.load(open(RECLIP))
    except Exception: tries = {}
    for b in db_all("SELECT * FROM blocks WHERE status='clipped' AND n_moments=0 AND coverage>=0.9"):
        if db_one("SELECT id FROM moments WHERE stream_id=? AND t_peak>=? AND t_peak<?", (b["stream_id"], b["start"], b["end"])): continue
        segs = db_all("SELECT * FROM segments WHERE stream_id=? AND verified=1 AND deleted=0 AND wall_end>? AND wall_start<?", (b["stream_id"], b["start"], b["end"]))
        if not segs: continue
        t = tries.get(b["id"], {"n": 0, "at": 0})
        if t["n"] >= 3:
            issues.append((b["stream_id"], f"block {b['idx'] + 1} of {b['stream_id']} still gives 0 clips after 3 tries: the file may have no sound"))
            continue
        if now() - t["at"] < 3600: continue
        for s in segs:   # drop empty sound-level caches so they are measured again
            for f in [s["path"] + ".rms.json"] + glob.glob(glob.escape(s["path"]) + ".rms.*.json"):
                try:
                    if os.path.getsize(f) <= 2: os.remove(f)
                except OSError: pass
        db_exec("UPDATE blocks SET status='complete' WHERE id=?", (b["id"],))
        tries[b["id"]] = {"n": t["n"] + 1, "at": now()}
        log(b["stream_id"], "INFO", f"health check: block {b['idx'] + 1} had 0 clips: sent back to clipping (try {t['n'] + 1} of 3)")
    json.dump(tries, open(RECLIP, "w"))

def loop(cfg):
    state = {}; tick = 0
    while True:
        try:
            if tick % 5 == 0:
                try: health_check(load_config())
                except Exception as e: log(None, "ERROR", f"health check: {type(e).__name__}: {e}")
            tick += 1
            out = {}
            for key, a in list(ACTIVE.items()):
                if a["proc"].poll() is not None: continue
                b = size(a["glob"]); s = state.get(key)
                t = time.time()
                if not s or s.get("t0") != a["t0"]:
                    s = state[key] = {"t0": a["t0"], "b": b, "t": t, "changed": t, "speed": 0.0}
                else:
                    dt = max(1.0, t - s["t"]); inst = max(0.0, (b - s["b"]) / dt)
                    s["speed"] = inst if not s["speed"] else 0.6 * s["speed"] + 0.4 * inst
                    if b > s["b"]: s["changed"] = t
                    s["b"], s["t"] = b, t
                stalled = t - s["changed"]
                exp = a.get("expected") or 0
                out[key] = {"sid": a["sid"], "kind": a["kind"], "label": a.get("label"), "bytes": b, "speed_bps": round(s["speed"]),
                            "expected": exp or None, "eta_s": round((exp - b) / s["speed"]) if exp > b and s["speed"] > 1000 else None,
                            "stalled_s": round(stalled), "since": iso(a["t0"])}
                if stalled > LIMIT[a["kind"]] and t - a["t0"] > LIMIT[a["kind"]]:
                    what = {"rec": "recording", "replay": "replay download", "fill": "missing-footage download"}[a["kind"]]
                    log(a["sid"], "ALERT", f"{what} was stuck for {int(stalled // 60)} min: restarting it")
                    a["killed"] = True; kill_tree(a["proc"]); s["changed"] = t
            # replay downloads still running from before a restart aren't registered: measure their files directly
            seen = {v["sid"] for v in out.values() if v["kind"] == "replay"}
            for f in glob.glob(os.path.join(RAW, "*", "*", "full_*.part")):
                parts = os.path.normpath(f).split(os.sep); sid = f"{parts[-3]}@{parts[-2]}"
                if sid in seen: continue
                key = f"replayfile:{sid}"; b = size(os.path.join(os.path.dirname(f), "full_*.part")); t = time.time()
                s = state.get(key) or {"b": b, "t": t, "speed": 0.0, "changed": t}
                dt = max(1.0, t - s["t"]); inst = max(0.0, (b - s["b"]) / dt)
                s["speed"] = inst if not s["speed"] else 0.6 * s["speed"] + 0.4 * inst
                if b > s["b"]: s["changed"] = t
                s["b"], s["t"] = b, t; state[key] = s
                st = db_one("SELECT * FROM streams WHERE id=?", (sid,))
                exp = int((st["bytes_per_s"] or 0) * ((st["ended_at"] or now()) - (st["broadcast_start"] or now()))) if st else 0
                if t - s["changed"] < 1800:
                    out[key] = {"sid": sid, "kind": "replay", "bytes": b, "speed_bps": round(s["speed"]), "expected": exp or None,
                                "eta_s": round((exp - b) / s["speed"]) if exp > b and s["speed"] > 1000 else None,
                                "stalled_s": round(t - s["changed"]), "since": None}
            tmp = OUT + ".tmp"; json.dump({"t": iso(now()), "items": out}, open(tmp, "w")); os.replace(tmp, OUT)
        except Exception as e:
            log(None, "ERROR", f"watchdog: {type(e).__name__}: {e}")
        time.sleep(60)
