"""Clip Studio API for the dashboard (SnipRiot). Mounted by dashboard.py, see STUDIO_SETUP.md.

GET  /studio, /app                  the Studio / phone app page (ui/studio.html)
GET  /manifest.webmanifest, /sw.js  makes it installable on a phone (PWA)
GET  /api/studio/moments            clips the editor can open (candidate cuts with their source video)
GET  /api/studio/projects           saved edits
GET  /api/studio/job?id=..          render progress
GET  /api/studio/feed               what the phone app shows on Home: alerts, live streams, review count
POST /api/studio/track  {src,in,out}            auto-reframe keyframes for one clip
POST /api/studio/save   {id?, name, edl}        save an edit (project)
POST /api/studio/render {id?, name, edl}        save + render in the background -> job id
POST /api/push/subscribe {subscription}         phone notifications (Web Push), see notify.py

Paths in an EDL are relative to the data folder (as /media/ URLs use them); they are checked to stay inside it.
"""
import json, os, glob, subprocess, sys, threading, time, uuid

from common import DATA_ROOT, DATA, SYSTEM_DIR, CANDIDATES, log
import worklog

# Extra free libraries live in tools/winlib2 (like the renderer's); installed once in the background if missing.
WINLIB = os.path.join(DATA_ROOT, "tools", "winlib2")
if os.path.isdir(WINLIB) and WINLIB not in sys.path:
    sys.path.append(WINLIB)
_tried = {}


def ensure(module, package):
    try:
        __import__(module); return True
    except Exception:
        pass
    if time.time() - _tried.get(package, 0) < 3600:
        return False
    _tried[package] = time.time()

    # a separate, detached pip process: a worker restart no longer kills a slow download half-way
    log(None, "INFO", f"studio: installing {package} in the background (one time)")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    lockf = os.path.join(DATA, f"pip_{package}.running")
    try:
        if os.path.exists(lockf) and time.time() - os.path.getmtime(lockf) < 3 * 3600:
            return False                                   # one already running from before a restart
        open(lockf, "w").close()
        logf = open(os.path.join(DATA, "logs", f"pip_{package}.log"), "a", encoding="utf-8")
        subprocess.Popen([sys.executable, "-m", "pip", "install", "--progress-bar", "off", "--target", WINLIB, package],
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    except Exception as e:
        log(None, "WARN", f"studio: couldn't start installing {package}: {e}")
    return False


try:   # use the same ffmpeg the rest of the system uses (it may not be on PATH)
    from common import load_config
    _c = load_config()
    studio_render.FFMPEG = _c.get("ffmpeg") or studio_render.FFMPEG
    studio_render.FFPROBE = _c.get("ffprobe") or studio_render.FFPROBE
except Exception:
    pass
ensure("cv2", "opencv-python-headless")
ensure("pywebpush", "pywebpush")
import studio_render

STUDIO = os.path.join(DATA_ROOT, "media", "studio")
UI = os.path.join(SYSTEM_DIR, "ui")
JOBS = {}
_lock = threading.Lock()


def _safe(rel):
    rel = (rel or "").replace("\\", "/")
    if rel.startswith("/media/"):
        rel = rel[7:]
    p = os.path.normpath(os.path.join(DATA_ROOT, rel))
    if not p.startswith(os.path.normpath(DATA_ROOT) + os.sep) or not os.path.isfile(p):
        raise ValueError(f"not a file in the data folder: {rel}")
    return p


def _rel(p):
    return os.path.relpath(p, DATA_ROOT).replace("\\", "/")


def moments(limit=400):
    out = []
    for p in glob.glob(os.path.join(CANDIDATES, "*", "*", "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("source"):
            continue
        if d.get("status") == "rejected":
            continue
        out.append({k: d.get(k) for k in ("id", "name", "stream_id", "day", "peak_time", "broadcast_offset", "length_s",
                                           "score_db", "source", "sheet", "status", "approved", "final", "tag", "hook")})
    out.sort(key=lambda d: (d.get("day") or "", d.get("peak_time") or ""), reverse=True)
    return out[:limit]


def projects():
    os.makedirs(STUDIO, exist_ok=True)
    items = []
    for p in glob.glob(os.path.join(STUDIO, "*.edl.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
            items.append({"id": d["id"], "name": d.get("name"), "saved": d.get("saved"), "output": d.get("output_rel"),
                          "duration": d.get("duration"), "clips": len(d.get("edl", {}).get("clips", []))})
        except Exception:
            pass
    return sorted(items, key=lambda d: d.get("saved") or "", reverse=True)


def save(body):
    pid = body.get("id") or time.strftime("short_%Y%m%d_%H%M%S")
    pid = "".join(ch for ch in pid if ch.isalnum() or ch in "_-")[:60]
    edl = body["edl"]
    for c in edl.get("clips", []):
        _safe(c["src"])                       # refuse anything outside the data folder
    if (edl.get("music") or {}).get("src"):
        _safe(edl["music"]["src"])
    os.makedirs(STUDIO, exist_ok=True)
    doc = {"id": pid, "name": body.get("name") or pid, "saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "edl": edl}
    old = os.path.join(STUDIO, pid + ".edl.json")
    if os.path.exists(old):
        try:
            prev = json.load(open(old, encoding="utf-8"))
            doc.update({k: prev[k] for k in ("output_rel", "duration") if k in prev})
        except Exception:
            pass
    json.dump(doc, open(old, "w", encoding="utf-8"), indent=1)
    return doc


def render(body):
    doc = save(body)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "project": doc["id"], "progress": 0.0, "step": "queued", "done": False, "error": None}

    def work():
        with _lock:                           # one render at a time: the laptop also records streams
            edl = json.loads(json.dumps(doc["edl"]))
            for c in edl["clips"]:
                c["src"] = _safe(c["src"])
            if (edl.get("music") or {}).get("src"):
                edl["music"]["src"] = _safe(edl["music"]["src"])
            out = os.path.join(STUDIO, doc["id"] + ".mp4")
            edl["output"] = out
            try:
                r = studio_render.render(edl, lambda p, m: JOBS[jid].update(progress=round(p, 3), step=m))
                doc.update(output_rel=_rel(out), duration=r["duration"])
                json.dump(doc, open(os.path.join(STUDIO, doc["id"] + ".edl.json"), "w", encoding="utf-8"), indent=1)
                JOBS[jid].update(done=True, progress=1.0, step="exported", output="/media/" + _rel(out))
                log(None, "INFO", f"studio: exported {doc['name']} ({r['duration']:.0f}s)")
            except Exception as e:
                JOBS[jid].update(done=True, error=str(e)[-400:])
                log(None, "WARN", f"studio: export of {doc['name']} failed: {e}")
    threading.Thread(target=work, daemon=True).start()
    return {"job": jid, "project": doc["id"]}


def feed():
    """Home screen of the phone app: newest alerts, health, live streams, clips waiting for review."""
    out = {"alerts": [], "health": None, "live": [], "review": 0}
    try:
        lines = open(os.path.join(DATA, "alerts.jsonl"), encoding="utf-8").read().splitlines()[-30:]
        out["alerts"] = [json.loads(x) for x in reversed(lines) if x.strip()]   # {ts, stream, msg}
    except Exception:
        pass
    try:
        out["health"] = json.load(open(os.path.join(DATA, "health.json"), encoding="utf-8"))
    except Exception:
        pass
    try:
        st = json.load(open(os.path.join(DATA, "status.json"), encoding="utf-8"))
        out["live"] = [{"name": s.get("name"), "title": s.get("title"), "id": s.get("id"), "url": s.get("url")}
                       for s in st.get("streams", []) if s.get("live_status") == "is_live"]
    except Exception:
        pass
    out["review"] = sum(1 for m in moments(2000) if m.get("status") == "final" and not m.get("approved"))
    return out


def _file(name, ctype):
    p = os.path.join(UI, name)
    try:
        return 200, open(p, "rb").read(), ctype
    except OSError:
        return 404, b"{}", "application/json"


def get(path, q):
    """Returns (code, body, content_type) or None when the path isn't ours."""
    if path in ("/studio", "/app", "/studio.html"):
        return _file("studio.html", "text/html; charset=utf-8")
    if path == "/manifest.webmanifest":
        return _file("manifest.webmanifest", "application/manifest+json")
    if path == "/sw.js":
        return _file("sw.js", "text/javascript")
    if path.startswith("/fonts/"):
        return _file(os.path.join("fonts", os.path.basename(path)), "font/ttf")
    if path.startswith("/icons/"):
        return _file(os.path.join("icons", os.path.basename(path)), "image/png")
    j = None
    if path == "/api/studio/moments": j = moments()
    elif path == "/api/studio/projects": j = projects()
    elif path == "/api/studio/feed": j = feed()
    elif path == "/api/studio/project":
        p = os.path.join(STUDIO, os.path.basename(q.get("id", "")) + ".edl.json")
        j = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"error": "no such project"}
    elif path == "/api/studio/job": j = JOBS.get(q.get("id"), {"error": "no such job"})
    elif path == "/api/studio/worklog": j = {"items": worklog.recent(120)}
    elif path == "/api/push/key":
        import notify; j = {"key": notify.public_key()}
    if j is None:
        return None
    return 200, json.dumps(j).encode(), "application/json"


def post(path, body):
    j = None
    if path == "/api/studio/track":
        keys = studio_render.track(_safe(body["src"]), float(body.get("in", 0)), float(body.get("out", 0)) or 1e9)
        j = {"keys": [{"t": t, "x": x} for t, x in keys]}
    elif path == "/api/studio/save": j = save(body)
    elif path == "/api/studio/render": j = render(body)
    elif path == "/api/push/subscribe":
        import notify; notify.subscribe(body.get("subscription")); j = {"ok": True}
    if j is None:
        return None
    return 200, json.dumps(j).encode(), "application/json"
