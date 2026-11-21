"""Local dashboard at http://localhost:8765 and data/status.json (read by Claude's scheduled runs)."""
import json, os, glob, sys, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from common import *
from pipeline import coverage, anchor

def status(cfg):
    out = {"generated": iso(now()), "free_gb": round(free_gb(), 1), "streams": []}
    for st in db_all("SELECT * FROM streams ORDER BY COALESCE(ended_at, 9e18) DESC, broadcast_start DESC"):
        sid = st["id"]; a0 = anchor(sid)
        end = st["ended_at"] or max(now(), a0 or now())
        cov = coverage(sid, a0, end, include_deleted=True) if a0 else []
        ondisk = coverage(sid, a0, end) if a0 else []
        gaps = db_all("SELECT start,end,status,note,next_try FROM gaps WHERE stream_id=? AND status!='filled' ORDER BY start", (sid,))
        waiting = [g["next_try"] for g in gaps if g["status"] in ("open", "retry")]
        next_check = max(0, round(min(waiting) - now())) if waiting else None
        blocks = db_all("SELECT idx,status,coverage,n_moments FROM blocks WHERE stream_id=? ORDER BY idx", (sid,))
        n_cand = db_one("SELECT COUNT(*) n FROM moments WHERE stream_id=?", (sid,))["n"]
        rel = lambda t: round(t - a0, 1) if a0 else None
        out["streams"].append({
            "id": sid, "name": st.get("name") or sid, "url": st.get("url"), "ended": iso(st.get("ended_at")),
            "title": st.get("title"), "streamer": sid.split("@")[0], "kind_note": st.get("note"), "live_status": st.get("live_status"), "recording": bool(st.get("recording")),
            "broadcast_start": iso(st.get("broadcast_start")), "now_offset": rel(end),
            "earliest_available": rel(st["earliest_available"]) if st.get("earliest_available") else None,
            "last_captured": rel(cov[-1][1]) if cov else None,
            "captured_s": round(sum(e - x for x, e in cov)), "captured": [[rel(x), rel(e)] for x, e in cov],
            "on_disk": [[rel(x), rel(e)] for x, e in ondisk],
            "gaps": [dict(g, start=rel(g["start"]), end=rel(g["end"]), next_try=None) for g in gaps],
            "blocks": blocks, "candidates": n_cand, "next_check_s": next_check,
            "rec_since": rel(st["rec_since"]) if st.get("recording") and st.get("rec_since") else None})
    try: dl = json.load(open(os.path.join(DATA, "downloads.json")))["items"]
    except Exception: dl = {}
    try:
        import backfill; qs = backfill.queues(cfg)
    except Exception: qs = {}
    for x in out["streams"]:
        x["transfers"] = [v for v in dl.values() if v.get("sid") == x["id"]]
        x["queue"] = qs.get(x["id"], {})
    out["manual"] = sorted(((cfg.get("backfill") or {}).get("skip_streams") or []))   # the owner downloads these himself
    out["events"] = db_all("SELECT ts,stream_id,level,msg FROM events ORDER BY id DESC LIMIT 40")
    for e in out["events"]: e["ts"] = iso(e["ts"])
    out["jobs"] = db_all("SELECT id,kind,params,status,result FROM jobs ORDER BY id DESC LIMIT 15")
    return out

UI_FILE = os.path.join(SYSTEM_DIR, "ui", "index.html")  # read on every request, so the UI can be updated without a restart
STARTED = now()
_tools = None

def tools(cfg):
    global _tools
    if _tools is None:
        out = {}
        for name, cmd in (("ffmpeg", [cfg["ffmpeg"], "-version"]), ("yt-dlp", cfg["ytdlp_cmd"] + ["--version"]),
                          ("python", [sys.executable, "--version"])):
            try: out[name] = (run(cmd, timeout=30).stdout or "").splitlines()[0][:80]
            except Exception as e: out[name] = f"missing ({type(e).__name__})"
        _tools = out
    return _tools

def read_json(p):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception: return None

def clips(kind="all", streamer=None, day=None, status=None, limit=3000):
    files = []
    if kind in ("all", "streamer"): files += glob.glob(os.path.join(CANDIDATES, "*", "*", "*.json"))
    if kind in ("all", "original"): files += [p for p in glob.glob(os.path.join(ORIGINALS, "*", "*.json")) if not p.endswith(".spec.json")]
    items = []
    for p in files:
        d = read_json(p)
        if not isinstance(d, dict): continue
        d.setdefault("id", os.path.splitext(os.path.basename(p))[0])
        d["kind"] = "original" if p.startswith(ORIGINALS) else "streamer"
        d["meta"] = os.path.relpath(p, DATA_ROOT).replace("\\", "/")
        d.setdefault("day", os.path.basename(os.path.dirname(p)))
        if streamer and d.get("stream_id", "").split("@")[0] != streamer: continue
        if day and d.get("day") != day: continue
        if status and d.get("status") != status: continue
        items.append(d)
    items.sort(key=lambda d: (d.get("day") or "", d.get("peak_time") or d.get("created") or ""), reverse=True)
    return items[:limit]

def queue(view):
    """review = finished clips waiting for the owner's decision; approved = the pool daily posts are picked from."""
    import captions
    items = [d for d in clips("all", status="final", limit=5000) if not (d.get("posted") and isinstance(d.get("posted"), dict) and len(d["posted"]) >= 3)]
    if view == "approved": items = [d for d in items if d.get("approved")]
    else: items = [d for d in items if not d.get("approved")]
    items.sort(key=lambda d: (d.get("approved_at") or d.get("day") or "", d.get("peak_time") or ""))
    for d in items:
        try: d["captions"] = d.get("captions") or captions.build(d)
        except Exception: pass
    return items

def set_clip(meta_rel, changes):
    p = os.path.normpath(os.path.join(DATA_ROOT, meta_rel))
    if not p.startswith(os.path.normpath(DATA_ROOT)) or not p.endswith(".json"): raise ValueError("bad path")
    d = read_json(p) or {}
    allowed = {"status", "title", "tag", "approved", "owner_note", "hook", "posted_check"}
    d.update({k: v for k, v in changes.items() if k in allowed}); d["updated"] = iso(now())
    if changes.get("approved") is True: d["approved_at"] = iso(now())
    if changes.get("approved") is False: d.pop("approved_at", None)
    if changes.get("status") == "removed": d["approved"] = False; d["removed_at"] = iso(now())
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, indent=1)
    os.replace(tmp, p); return d

def delete_clip(meta_rel):
    """Permanently delete a rejected clip: its cut, contact sheet, edited video and record. The moment stays listed in
    the database, so the same moment is never cut again."""
    p = os.path.normpath(os.path.join(DATA_ROOT, meta_rel))
    if not p.startswith(os.path.normpath(DATA_ROOT)) or not p.endswith(".json"): raise ValueError("bad path")
    d = read_json(p) or {}
    if d.get("status") not in ("rejected", "removed"): raise ValueError("only rejected clips can be deleted")
    gone = 0
    for k in ("source", "sheet", "final"):
        if d.get(k):
            f = os.path.normpath(os.path.join(DATA_ROOT, d[k]))
            if f.startswith(os.path.normpath(DATA_ROOT)) and os.path.isfile(f):
                os.remove(f); gone += 1
    os.remove(p)
    log(None, "INFO", f"deleted rejected clip {d.get('id')} ({gone} files)")
    return {"deleted": d.get("id")}

import hashlib, random, socket, http.cookies

def phone_cfg():
    c = load_config().get("phone") or {}
    if not c.get("pin"):
        c = {"enabled": c.get("enabled", False), "pin": f"{random.SystemRandom().randint(0, 999999):06d}"}
        save_settings({"phone": c})
    return c

def lan_ip():
    try:
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s_.connect(("8.8.8.8", 80)); ip = s_.getsockname()[0]; s_.close(); return ip
    except Exception: return None

def pin_token(pin): return hashlib.sha256(("snipriot:" + pin).encode()).hexdigest()[:32]

LOGIN_HTML = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=theme-color content="#0d0816"><title>SnipRiot Studio</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Anton&display=swap">
<body style="background:radial-gradient(900px 400px at 10% -10%,#2a0f3a 0,transparent 60%),#0d0816;color:#f4eefc;font-family:system-ui,-apple-system,Segoe UI,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0">
<form method=post action=/login style="background:#150e22;border:1px solid #2a1f3d;padding:28px 24px;border-radius:16px;width:min(320px,88vw);text-align:center">
<div style="font-family:Anton,Impact,sans-serif;font-size:34px;line-height:1;letter-spacing:.01em">SNIPRIOT STUDIO</div>
<p style="color:#8d80a6;margin:8px 0 18px;font-size:14px">Enter your PIN</p>
<input name=pin type=password inputmode=numeric autocomplete=current-password autofocus style="box-sizing:border-box;width:100%;padding:12px;font-size:22px;text-align:center;border-radius:10px;border:1px solid #2a1f3d;background:#0d0816;color:#fff">
<button style="margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;background:#ff4fa3;color:#1a0612;font-weight:700;font-size:16px">Open</button>@@MSG@@</form>"""

def insights():
    import posts
    rows = posts.summary(); by = {}
    for r in rows:
        v = (r.get("h24") or r.get("latest") or {}).get("views")
        if v is None: continue
        meta = read_json(os.path.join(DATA_ROOT, r["clip_meta"])) if r.get("clip_meta") else {}
        meta = meta or {}
        keys = [("Streamer", r.get("streamer") or meta.get("name") or "Original"), ("Platform", r["platform"])]
        keys += [("Topic", "#" + t.lstrip("#")) for t in (meta.get("topic_tags") or [])]
        L = meta.get("length_s")
        if L: keys.append(("Length", "under 25 s" if L < 25 else "25–40 s" if L <= 40 else "over 40 s"))
        if meta.get("kind") == "original": keys.append(("Type", "Original"))
        elif meta: keys.append(("Type", "Streamer clip"))
        for k in keys: by.setdefault(k, []).append(v)
    out = [{"group": g, "value": val, "posts": len(vs), "avg_views": round(sum(vs) / len(vs))} for (g, val), vs in by.items()]
    out.sort(key=lambda x: (x["group"], -x["avg_views"]))
    return out

def picks_zip(day):
    import zipfile, tempfile
    p = os.path.join(DATA, "daily_picks", day + ".json"); d = read_json(p) or {}
    items = d if isinstance(d, list) else d.get("items") or []
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False); tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
        lines = []
        for i, it in enumerate(items, 1):
            f = os.path.join(DATA_ROOT, it.get("final") or "")
            name = f"{i:02d}_{(it.get('time') or '').replace(':', '')}_{it.get('id', 'video')}.mp4"
            if os.path.isfile(f): z.write(f, name)
            c = it.get("captions") or {}
            lines += [f"#{i} {it.get('time', '')}  {it.get('title', '')}  ({name})", "", "TikTok:", c.get("tiktok", ""), "",
                      "YouTube title:", c.get("youtube_title", ""), "YouTube description:", c.get("youtube", ""), "", "X:", c.get("x", ""), "", "-" * 40, ""]
        z.writestr("captions.txt", "\n".join(lines))
    return tmp.name

def picks():
    out = []
    for p in sorted(glob.glob(os.path.join(DATA, "daily_picks", "*.json")), reverse=True)[:14]:
        d = read_json(p)
        try:
            import captions
            items = d if isinstance(d, list) else (d or {}).get("items") or (d or {}).get("picks") or (d or {}).get("videos") or []
            for it in items:
                if isinstance(it, dict) and not it.get("captions"):
                    rec = dict(it)
                    if it.get("meta"):
                        rec = {**(read_json(os.path.join(DATA_ROOT, it["meta"])) or {}), **it}
                    it["captions"] = captions.build(rec)
            for it in items:
                if isinstance(it, dict) and it.get("meta"):
                    sc = read_json(os.path.join(DATA_ROOT, it["meta"])) or {}
                    it["posted_check"] = sc.get("posted_check") or {}; it["posted"] = sc.get("posted") or {}
        except Exception as e:
            log(None, "WARN", f"captions: {e}")
        out.append({"day": os.path.splitext(os.path.basename(p))[0], "file": os.path.relpath(p, DATA_ROOT), "data": d})
    return out

def system(cfg):
    hb = None
    try: hb = open(os.path.join(DATA, "heartbeat")).read().strip()
    except Exception: pass
    return {"tools": tools(cfg), "free_gb": round(free_gb(), 1), "data_root": DATA_ROOT, "code_root": ROOT,
            "heartbeat": hb, "uptime_s": round(now() - STARTED), "backfill_paused": os.path.exists(os.path.join(DATA, "pause_backfill")),
            "disk_guard": os.path.exists(os.path.join(DATA, "pause_backfill_disk")), "port": cfg["dashboard_port"]}

EDITABLE = {"max_height": int, "segment_minutes": int, "block_hours": float, "raw_keep_hours_after_clip": float, "min_free_gb": float}

POST_ORIGINS = {"https://studio.youtube.com", "https://www.youtube.com", "https://www.tiktok.com", "https://x.com", "https://twitter.com"}

def serve(cfg):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
        def _json(self, obj, code=200): self._send(code, json.dumps(obj))
        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        def _media(self, rel):
            p = os.path.normpath(os.path.join(DATA_ROOT, rel))
            if not p.startswith(os.path.normpath(DATA_ROOT)) or not os.path.isfile(p): return self._send(404, "{}")
            size = os.path.getsize(p); ctype = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png"}.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
            start, end = 0, size - 1; rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-"); start = int(a or 0); end = min(int(b) if b else size - 1, size - 1)
                self.send_response(206); self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype); self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start); left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk: break
                    try: self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError): return
                    left -= len(chunk)
        def _cors(self):
            # lets the posting pages (and only them) pull a finished video from /media/ to attach it to a post
            o = self.headers.get("Origin") or ""
            if o in POST_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", o); self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Private-Network", "true")
        def do_OPTIONS(self):
            self.send_response(204); self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET"); self.send_header("Access-Control-Allow-Headers", "Range")
            self.send_header("Content-Length", "0"); self.end_headers()
        def _local(self):   # Tailscale Serve / any proxy connects from 127.0.0.1 too: those are NOT local
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and not (self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login"))
        def _authed(self):
            if self._local(): return True
            pc = phone_cfg()
            if not pc.get("enabled"): return False
            ck = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
            return "sr" in ck and ck["sr"].value == pin_token(pc["pin"])
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs, unquote
            u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._authed():
                if self.path.startswith("/api/") or self.path.startswith("/media/"): return self._send(401, "{}")
                return self._send(200, LOGIN_HTML.replace("@@MSG@@", ""), "text/html; charset=utf-8")
            try:
                if u.path == "/api/phone":
                    pc = phone_cfg(); ip = lan_ip()
                    return self._json({"enabled": bool(pc.get("enabled")), "pin": pc["pin"] if self._local() else None,
                                       "url": f"http://{ip}:{cfg['dashboard_port']}" if ip else None})
                if u.path == "/api/insights": return self._json(insights())
                if u.path == "/api/picks/zip":
                    day = "".join(ch for ch in q.get("day", "") if ch.isdigit() or ch == "-")
                    z = picks_zip(day)
                    try:
                        size = os.path.getsize(z); self.send_response(200)
                        self.send_header("Content-Type", "application/zip"); self.send_header("Content-Length", str(size))
                        self.send_header("Content-Disposition", f'attachment; filename="SnipRiot_{day}.zip"'); self.end_headers()
                        with open(z, "rb") as f:
                            while True:
                                b = f.read(1 << 20)
                                if not b: break
                                self.wfile.write(b)
                    finally:
                        try: os.remove(z)
                        except OSError: pass
                    return
                if u.path in ("/", "/index.html"):
                    try: html = open(UI_FILE, encoding="utf-8").read()
                    except OSError: html = "<h1>SnipRiot</h1><p>UI file missing: system/ui/index.html</p>"
                    return self._send(200, html, "text/html; charset=utf-8")
                if u.path == "/api/status": return self._json(status(load_config()))
                if u.path == "/api/config":
                    c = load_config(); c["backfill"].setdefault("recheck_minutes", 30)
                    rl = str(c["backfill"].get("rate_limit") or ""); c["backfill"]["rate_limit_mb"] = float(rl[:-1]) if rl.endswith("M") else 0
                    return self._json({"streams": c["streams"], **{k: c.get(k) for k in EDITABLE},
                                                          "moment": c["moment"], "backfill": c["backfill"]})
                if u.path == "/api/clips": return self._json(clips(q.get("kind", "all"), q.get("streamer"), q.get("day"), q.get("status")))
                if u.path == "/api/queue": return self._json(queue(q.get("view", "review")))
                if u.path == "/api/picks": return self._json(picks())
                if u.path == "/api/posts":
                    import posts; return self._json(posts.summary())
                if u.path == "/api/socials": return self._json(load_config().get("socials") or {})
                if u.path == "/api/system": return self._json(system(load_config()))
                import studio_api   # SnipRiot Studio + phone app
                _r = studio_api.get(u.path, q)
                if _r: return self._send(*_r)
                if u.path.startswith("/media/"): return self._media(unquote(u.path[7:]))
                self._send(404, "{}")
            except Exception as e:
                self._json({"error": str(e)}, 500)
        def do_POST(self):
            if self.path == "/login":
                from urllib.parse import parse_qs
                n = int(self.headers.get("Content-Length", 0) or 0); f = parse_qs(self.rfile.read(n).decode())
                pc = phone_cfg()
                if pc.get("enabled") and (f.get("pin") or [""])[0].strip() == pc["pin"]:
                    self.send_response(303); _ref = (self.headers.get("Referer") or "").split("://")[-1].partition("/")[2]  # back to the app if signed in from it
                    self.send_header("Location", "/app" if _ref.startswith(("app", "studio")) else "/")
                    self.send_header("Set-Cookie", f"sr={pin_token(pc['pin'])}; Path=/; HttpOnly; Max-Age=31536000; SameSite=Lax")
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                time.sleep(1.5); return self._send(200, LOGIN_HTML.replace("@@MSG@@", "<p style='color:#ff6b6b;margin:12px 0 0'>Wrong PIN</p>"), "text/html; charset=utf-8")
            if not self._authed(): return self._send(401, "{}")
            try:
                d = self._body()
                import studio_api   # SnipRiot Studio + phone app
                _r = studio_api.post(self.path, d)
                if _r: return self._send(*_r)
                if self.path == "/api/phone":
                    if not self._local(): return self._send(403, "{}")
                    pc = phone_cfg()
                    if d.get("new_pin"): pc["pin"] = f"{random.SystemRandom().randint(0, 999999):06d}"
                    if "enabled" in d: pc["enabled"] = bool(d["enabled"])
                    save_settings({"phone": pc}); log(None, "INFO", f"phone access {'on' if pc['enabled'] else 'off'}")
                    if "enabled" in d: open(os.path.join(DATA, "restart_request"), "w").close()   # re-open the port for the network
                    return self._json({"ok": True, "restart": True})
                if self.path == "/api/jobs":
                    assert d["kind"] in ("cut", "capture", "reprocess", "add_video")
                    db_exec("INSERT INTO jobs(kind,params,status,created) VALUES(?,?,'queued',?)", (d["kind"], json.dumps(d["params"]), now()))
                    return self._json({"ok": True})
                if self.path == "/api/streams":  # full list from the Streams page
                    streams = []
                    for s in d["streams"]:
                        sid = "".join(ch for ch in s["id"].lower() if ch.isalnum() or ch in "-_")[:24]
                        u = s.get("url", "").strip()
                        if not sid or not u.startswith(("https://", "http://")) or " " in u:   # YouTube, Twitch, Kick, any live link
                            return self._json({"error": f"'{s.get('name') or sid}': paste the full live link (starting with https://)"}, 400)
                        streams.append({"id": sid, "name": s["name"].strip()[:40] or sid, "url": s["url"].strip(),
                                        "credit": s.get("credit", "").strip()[:40], "enabled": bool(s.get("enabled", True))})
                    if len({s["id"] for s in streams}) != len(streams): return self._json({"error": "two streamers share an id"}, 400)
                    old = {s["id"]: s for s in (load_config().get("streams") or [])}
                    if old and not streams:   # 2026-11-21: an empty save wiped every streamer and stopped clipping
                        return self._json({"error": "That would remove every streamer. Reload the page and try again."}, 400)
                    streams = [{**old.get(s["id"], {}), **s} for s in streams]   # keep campaign, backup and other settings the page doesn't show
                    save_settings({"streams": streams}); log(None, "INFO", f"streams updated from the control centre ({len(streams)})")
                    return self._json({"ok": True})
                if self.path == "/api/settings":
                    ch = {k: EDITABLE[k](v) for k, v in d.items() if k in EDITABLE}
                    if "moment" in d: ch["moment"] = {k: float(v) for k, v in d["moment"].items() if k in ("threshold_db", "max_per_block", "pre_s", "post_s", "min_gap_s")}
                    if "backfill" in d:
                        b = d["backfill"]; ch["backfill"] = {"enabled": bool(b.get("enabled", True))}
                        if b.get("recheck_minutes") not in (None, ""): ch["backfill"]["recheck_minutes"] = max(1.0, min(1440.0, float(b["recheck_minutes"])))
                        if b.get("rate_limit_mb") not in (None, ""):
                            mb = float(b["rate_limit_mb"]); ch["backfill"]["rate_limit"] = f"{mb:g}M" if mb > 0 else None
                    save_settings(ch); log(None, "INFO", "settings updated from the control centre")
                    return self._json({"ok": True, "restart_needed": any(k in ch for k in ("max_height", "segment_minutes"))})
                if self.path == "/api/posts":
                    import posts
                    if d.get("action") == "remove": return self._json(posts.remove(int(d["id"])))
                    r = posts.add(d, by="owner"); return self._json(r, 400 if r.get("error") else 200)
                if self.path == "/api/socials":
                    import posts
                    soc = {}
                    for k, v in (d or {}).items():
                        if k in posts.PLATFORMS:
                            url = (v.get("url") or "").strip()
                            if url and not url.startswith("https://"): return self._json({"error": f"{k}: use the full https:// link to your profile"}, 400)
                            soc[k] = {"url": url, "handle": (v.get("handle") or "").strip()[:40], "enabled": bool(v.get("enabled"))}
                    save_settings({"socials": soc}); log(None, "INFO", "social accounts updated from the control centre")
                    return self._json({"ok": True})
                if self.path == "/api/clip":
                    if d.get("action") == "delete":
                        metas = d.get("metas") or [d["meta"]]
                        out = [delete_clip(m) for m in metas]; return self._json({"deleted": len(out)})
                    if (d.get("changes") or {}).get("status") == "duplicate":
                        set_clip(d["meta"], {"status": "rejected"}); return self._json(delete_clip(d["meta"]))
                    return self._json(set_clip(d["meta"], d.get("changes", {})))
                if self.path == "/api/control":
                    a = d.get("action"); flag = os.path.join(DATA, "pause_backfill")
                    if a == "pause_backfill": open(flag, "w").close()
                    elif a == "resume_backfill":
                        if os.path.exists(flag): os.remove(flag)
                    elif a == "restart": open(os.path.join(DATA, "restart_request"), "w").close()
                    else: return self._json({"error": "unknown action"}, 400)
                    log(None, "INFO", f"control centre: {a}"); return self._json({"ok": True})
                self._send(404, "{}")
            except Exception as e:
                self._json({"error": str(e)}, 400)
    host = "0.0.0.0" if (load_config().get("phone") or {}).get("enabled") else "127.0.0.1"
    import ssl, socket as _sk
    cert, key = os.path.join(DATA, "tls", "cert.pem"), os.path.join(DATA, "tls", "key.pem")
    ctx = None
    if os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    class Srv(ThreadingHTTPServer):
        daemon_threads = True
        def finish_request(self, request, client_address):
            # phone browsers (Chrome, Brave) insist on https: the same port answers both http:// and https://
            if ctx:
                try:
                    request.settimeout(10)
                    if request.recv(1, _sk.MSG_PEEK) == b"\x16":
                        request = ctx.wrap_socket(request, server_side=True)
                    request.settimeout(None)
                except Exception:
                    return
            self.RequestHandlerClass(request, client_address, self)
    class Srv6(Srv):   # IPv6 + IPv4 on one socket: Windows' "localhost" (used by Tailscale Serve) is often ::1
        address_family = _sk.AF_INET6
        def server_bind(self):
            try: self.socket.setsockopt(_sk.IPPROTO_IPV6, _sk.IPV6_V6ONLY, 0)
            except Exception: pass
            super().server_bind()
    try: srv = Srv6(("::", cfg["dashboard_port"]), H) if host == "0.0.0.0" else Srv((host, cfg["dashboard_port"]), H)
    except Exception as e:
        log(None, "WARN", f"dashboard: IPv6 not available ({e}); IPv4 only"); srv = Srv((host, cfg["dashboard_port"]), H)
    srv.serve_forever()

def write_status_loop(cfg):
    while True:
        try:
            tmp = os.path.join(DATA, "status.json.tmp")
            json.dump(status(cfg), open(tmp, "w", encoding="utf-8"), indent=1)
            os.replace(tmp, os.path.join(DATA, "status.json"))
        except Exception as e:
            log(None, "ERROR", f"status: {e}")
        time.sleep(60)
