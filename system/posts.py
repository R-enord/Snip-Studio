"""Post tracking: every video posted to a social platform, with its public stats over time.
Stats come from the public post page via yt-dlp (no API keys, no cost): views, likes, comments, reposts.
Snapshots are taken hourly for the first 3 days, then every 6 hours until day 8."""
import json, time
from common import *

PLATFORMS = ("youtube", "tiktok", "x", "instagram", "threads")

def init():
    with_db = """CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT, clip_meta TEXT, title TEXT,
      streamer TEXT, platform TEXT, url TEXT UNIQUE, posted_at REAL, added_by TEXT, snapshots TEXT DEFAULT '[]',
      last_checked REAL DEFAULT 0, note TEXT)"""
    db_exec(with_db)

def guess_platform(url):
    u = url.lower()
    for k, p in (("youtu", "youtube"), ("tiktok", "tiktok"), ("x.com", "x"), ("twitter", "x"), ("instagram", "instagram"), ("threads", "threads")):
        if k in u: return p
    return None

def add(params, by="owner"):
    url = (params.get("url") or "").strip()
    if not url.startswith("https://"): return {"error": "paste the post's link (https://...)"}
    plat = params.get("platform") or guess_platform(url)
    if plat not in PLATFORMS: return {"error": "unknown platform for this link"}
    if db_one("SELECT id FROM posts WHERE url=?", (url,)): return {"error": "this post is already tracked"}
    meta = params.get("meta") or ""; title = params.get("title") or ""; clip_id = params.get("clip_id") or ""; streamer = params.get("streamer") or ""
    if meta:
        try:
            d = json.load(open(os.path.join(DATA_ROOT, meta), encoding="utf-8"))
            title = title or d.get("title") or ""; clip_id = clip_id or d.get("id", ""); streamer = streamer or d.get("name") or ""
            d.setdefault("posted", {}); d["posted"] = d["posted"] if isinstance(d["posted"], dict) else {}
            d["posted"][plat] = url
            tmp = os.path.join(DATA_ROOT, meta) + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"), indent=1); os.replace(tmp, os.path.join(DATA_ROOT, meta))
        except Exception as e:
            log(None, "WARN", f"post: couldn't update clip record {meta}: {e}")
    posted_at = float(params.get("posted_at") or now())
    db_exec("INSERT INTO posts(clip_id,clip_meta,title,streamer,platform,url,posted_at,added_by) VALUES(?,?,?,?,?,?,?,?)",
            (clip_id, meta, title, streamer, plat, url, posted_at, by))
    log(None, "INFO", f"tracking {plat} post: {title or url}")
    return {"tracking": url, "platform": plat}

def remove(pid):
    db_exec("DELETE FROM posts WHERE id=?", (pid,)); return {"removed": pid}

def fetch_stats(cfg, url):
    r = run(cfg["ytdlp_cmd"] + ["-J", "--no-warnings", "--skip-download", "--no-playlist", url], timeout=120)
    if r.returncode != 0: raise RuntimeError((r.stderr or "").strip()[-200:])
    d = json.loads(r.stdout)
    g = lambda *ks: next((d.get(k) for k in ks if d.get(k) is not None), None)
    return {"t": now(), "views": g("view_count"), "likes": g("like_count"), "comments": g("comment_count"),
            "shares": g("repost_count", "share_count")}

def due(p):
    age = now() - p["posted_at"]
    if age > 8 * 86400: return False
    every = 3600 if age < 3 * 86400 else 6 * 3600
    return now() - (p["last_checked"] or 0) >= every

def loop(cfg):
    init()
    while True:
        try:
            cfg = load_config()
            for p in db_all("SELECT * FROM posts ORDER BY posted_at DESC"):
                if not due(p): continue
                try:
                    snap = fetch_stats(cfg, p["url"])
                    snaps = json.loads(p["snapshots"] or "[]") + [snap]
                    db_exec("UPDATE posts SET snapshots=?, last_checked=?, note=NULL WHERE id=?", (json.dumps(snaps[-200:]), now(), p["id"]))
                except Exception as e:
                    db_exec("UPDATE posts SET last_checked=?, note=? WHERE id=?", (now(), f"stats unavailable: {str(e)[:120]}", p["id"]))
                time.sleep(3)
            write_scoreboard()
        except Exception as e:
            log(None, "ERROR", f"posts: {type(e).__name__}: {e}")
        time.sleep(300)

def at_age(snaps, posted_at, hours):
    """The snapshot closest to <hours> after posting (within +-50%), or None if that age isn't reached yet."""
    target = posted_at + hours * 3600
    if now() < target - 600: return None
    best = min(snaps, key=lambda s: abs(s["t"] - target), default=None)
    return best if best and abs(best["t"] - target) <= hours * 1800 + 1800 else None

def summary():
    out = []
    for p in db_all("SELECT * FROM posts ORDER BY posted_at DESC"):
        snaps = json.loads(p["snapshots"] or "[]")
        last = snaps[-1] if snaps else {}
        row = {k: p[k] for k in ("id", "clip_id", "clip_meta", "title", "streamer", "platform", "url", "added_by", "note")}
        row.update(posted_at=iso(p["posted_at"]), latest=last, last_checked=iso(p["last_checked"]) if p["last_checked"] else None,
                   h12=at_age(snaps, p["posted_at"], 12), h24=at_age(snaps, p["posted_at"], 24), h72=at_age(snaps, p["posted_at"], 72))
        out.append(row)
    return out

def write_scoreboard():
    rows = summary()
    p = os.path.join(DATA, "scoreboard.csv")
    cols = ["post_id", "date_time", "platform", "streamer", "title", "url", "views_12h", "views_24h", "views_72h", "views_now", "likes", "comments", "shares"]
    with open(p + ".tmp", "w", encoding="utf-8", newline="") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            v = lambda s: "" if not s or s.get("views") is None else str(s["views"])
            L = r["latest"] or {}
            vals = [r["id"], r["posted_at"], r["platform"], r["streamer"], (r["title"] or "").replace(",", " "), r["url"],
                    v(r["h12"]), v(r["h24"]), v(r["h72"]), v(L), L.get("likes", ""), L.get("comments", ""), L.get("shares", "")]
            f.write(",".join("" if x is None else str(x) for x in vals) + "\n")
    os.replace(p + ".tmp", p)
