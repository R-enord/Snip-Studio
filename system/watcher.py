"""Channel watcher: no links needed from the owner.
Every few minutes it reads each streamer's public YouTube Streams list (and the Videos list for long uploads) and:
  - switches the recorder to a new GTA 6 live or upcoming stream as soon as it appears;
  - adds finished GTA 6 streams/uploads we don't have yet as their own timelines (downloaded and clipped like any
    stream); a stream that's private for now is picked up the moment it becomes public.
Only GTA titles are followed. Videos the owner removed (or that we deleted as duplicates) are never re-added.
Settings (data/settings.json, "watcher"): enabled, every_minutes, add_past (true), since (date: only add past streams
published on/after it), title_match (regex)."""
import json, os, re, time, datetime
from common import *

KNOWN = os.path.join(DATA, "known_videos.json")

def known():
    try: return set(json.load(open(KNOWN, encoding="utf-8")))
    except Exception: return set()

def remember(ids):
    k = known() | set(ids); tmp = KNOWN + ".tmp"
    json.dump(sorted(k), open(tmp, "w", encoding="utf-8")); os.replace(tmp, KNOWN)

def flat(cfg, url, n=12):
    r = run(cfg["ytdlp_cmd"] + ["--flat-playlist", "-J", "--no-warnings", "--playlist-end", str(n), "--socket-timeout", "30", url], timeout=240)
    if r.returncode != 0: raise RuntimeError((r.stderr or "").strip()[-200:])
    return json.loads(r.stdout).get("entries") or []

def vid_of(url):
    m = re.search(r"(?:v=|/live/|youtu\.be/)([A-Za-z0-9_-]{11})", url or ""); return m.group(1) if m else None

def channel_of(cfg, s):
    if s.get("channel"): return s["channel"]
    from recorder import fetch_info
    info = fetch_info(cfg, s["url"])
    ch = info.get("channel_url") or (f"https://www.youtube.com/channel/{info['channel_id']}" if info.get("channel_id") else None)
    if ch: set_stream(s["id"], channel=ch); log(None, "INFO", f"watcher: following {s['name']}'s channel {ch}")
    return ch

def set_stream(sid, **kv):
    cur = {}
    if os.path.exists(SETTINGS): cur = json.load(open(SETTINGS, encoding="utf-8"))
    streams = cur.get("streams") or [dict(x) for x in load_config()["streams"]]
    for x in streams:
        if x["id"] == sid: x.update(kv)
    cur["streams"] = streams
    tmp = SETTINGS + ".tmp"; json.dump(cur, open(tmp, "w", encoding="utf-8"), indent=1); os.replace(tmp, SETTINGS)

def check_twitch(cfg, s, W, login):
    """Twitch streamers: the recorder watches the channel live; here we add finished GTA broadcasts (archives) we
    don't have yet, one per check. The archive of a broadcast we recorded live is that timeline's own link, so skipped."""
    if not W.get("add_past", True): return
    match = re.compile(W.get("title_match", r"GTA ?(6|VI)\b|GTA ?VI|Grand Theft Auto (6|VI)\b|Leonida|Vice City"), re.I)
    not_six = re.compile(r"GTA ?(5|V|IV|4|San Andreas)\b|Grand Theft Auto (5|V|IV|4|San Andreas)\b|\bRP\b|roleplay", re.I)
    skip = re.compile(W.get("skip_match", r"trailer|reaction|react|podcast"), re.I)
    rows = db_all("SELECT video_id, url, broadcast_start, note FROM streams WHERE source_id=?", (s["id"],))
    have = {r["video_id"] for r in rows} | {(r["url"] or "").rstrip("/") for r in rows} | known()
    live_starts = [r["broadcast_start"] for r in rows if r["broadcast_start"] and (r["note"] or "") != "added video"]
    since = W.get("since", "2026-11-18")
    for e in flat(cfg, f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time", 12):
        vid, title, url = str(e.get("id") or ""), e.get("title") or "", (e.get("url") or "").rstrip("/")
        if not vid or vid in have or url in have or not match.search(title) or not_six.search(title) or skip.search(title): continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        if ts and datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") < since: remember([vid]); continue
        if ts and any(abs(ts - b) < 3600 for b in live_starts): remember([vid]); continue   # we recorded this one live
        from pipeline import add_video
        r = add_video(cfg, {"streamer": s["id"], "url": url or f"https://www.twitch.tv/videos/{vid.lstrip('v')}"})
        if r.get("added"):
            log(s["id"], "ALERT", f"found {s['name']}'s finished Twitch stream '{title[:80]}' ({r.get('length')}): downloading it to clip")
            remember([vid]); return
        if "couldn't read" in (r.get("error") or ""): continue
        remember([vid])

def check(cfg, s, W):
    from recorder import twitch_login
    login = twitch_login(s.get("channel") or s["url"])
    if login: return check_twitch(cfg, s, W, login)
    ch = channel_of(cfg, s)
    if not ch: return
    match = re.compile(W.get("title_match", r"GTA ?(6|VI)\b|GTA ?VI|Grand Theft Auto (6|VI)\b|Leonida|Vice City"), re.I)
    not_six = re.compile(r"GTA ?(5|V|IV|4|San Andreas)\b|Grand Theft Auto (5|V|IV|4|San Andreas)\b", re.I)
    have = {r["video_id"] for r in db_all("SELECT video_id FROM streams WHERE source_id=?", (s["id"],))} | known()
    entries = flat(cfg, ch.rstrip("/") + "/streams")
    cur_id = vid_of(s["url"])
    cur = db_one("SELECT live_status FROM streams WHERE id=?", (f"{s['id']}@{cur_id}",)) if cur_id else None
    cur_live = bool(cur and cur["live_status"] in ("is_live", "is_upcoming"))
    for e in entries:   # newest first
        vid, title, ls = e.get("id"), e.get("title") or "", e.get("live_status")
        if not vid or vid in have or not match.search(title) or not_six.search(title): continue
        if ls in ("is_live", "is_upcoming") and vid != cur_id:
            if cur_live and ls == "is_upcoming": continue          # don't leave a live stream for a scheduled one
            set_stream(s["id"], url=f"https://www.youtube.com/watch?v={vid}")
            log(s["id"], "ALERT", f"found {s['name']}'s {'new live' if ls == 'is_live' else 'upcoming'} stream '{title[:80]}': "
                                   f"{'recording it now' if ls == 'is_live' else 'it will record as soon as it starts'}")
            return
    if not W.get("add_past", True) or s.get("add_past") is False: return   # per streamer: live only, no finished uploads
    since = W.get("since", "2026-11-18")
    skip = re.compile(W.get("skip_match", r"trailer|reaction|react|podcast"), re.I)
    longs = [e for e in entries[:5] if e.get("live_status") in ("was_live", "post_live") and not skip.search(e.get("title") or "")]
    try:
        longs += [e for e in flat(cfg, ch.rstrip("/") + "/videos", 6) if (e.get("duration") or 0) >= 3600 and not skip.search(e.get("title") or "")]
    except Exception: pass
    for e in longs:
        vid, title = e.get("id"), e.get("title") or ""
        if not vid or vid in have or not match.search(title) or not_six.search(title): continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        if ts and datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") < since: remember([vid]); continue
        from pipeline import add_video
        r = add_video(cfg, {"streamer": s["id"], "url": f"https://www.youtube.com/watch?v={vid}"})
        if r.get("added"):
            log(s["id"], "ALERT", f"found {s['name']}'s finished stream '{title[:80]}' ({r.get('length')}): downloading it to clip")
            remember([vid]); return                                  # one per check, so downloads don't pile up at once
        if "private" in (r.get("error") or "") or "couldn't read" in (r.get("error") or ""):
            continue                                                 # private / processing: try again next time
        remember([vid])

def loop(cfg):
    time.sleep(90)
    while True:
        cfg = load_config(); W = cfg.get("watcher") or {}
        if W.get("enabled", True):
            for s in cfg["streams"]:
                if not s.get("enabled", True): continue
                try: check(cfg, s, W)
                except Exception as e: log(s["id"], "WARN", f"watcher: couldn't read {s['name']}'s channel: {str(e)[:160]}")
                time.sleep(5)
        time.sleep(60 * int(W.get("every_minutes", 5)))
