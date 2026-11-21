"""Shared helpers: config, paths, SQLite timeline, logging, subprocess."""
import json, os, sqlite3, subprocess, threading, time, datetime, shutil

SYSTEM_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SYSTEM_DIR)                     # ...\Videos\SnipRiot (code, brand, playbook)
# Recordings, clips and the timeline live outside Windows' protected folders (Videos is blocked
# for unknown apps by Controlled folder access), in config "data_root" (default %USERPROFILE%\SnipRiot).
try:
    _dr = json.load(open(os.environ.get("SNIPRIOT_CONFIG", os.path.join(SYSTEM_DIR, "config.json")), encoding="utf-8")).get("data_root")
except Exception:
    _dr = None
DATA_ROOT = os.path.abspath(os.path.expandvars(os.path.expanduser(_dr))) if _dr else ROOT
DATA = os.path.join(DATA_ROOT, "data")
RAW = os.path.join(DATA_ROOT, "media", "raw")
CANDIDATES = os.path.join(DATA_ROOT, "media", "candidates")
LIBRARY = os.path.join(DATA_ROOT, "media", "library")
MANUAL = os.path.join(DATA_ROOT, "media", "manual")
DB_PATH = os.path.join(DATA, "timeline.db")
ALERTS = os.path.join(DATA, "alerts.jsonl")
ORIGINALS = os.path.join(DATA_ROOT, "media", "originals")
for d in (DATA, RAW, CANDIDATES, LIBRARY, MANUAL, ORIGINALS, os.path.join(DATA, "logs"), os.path.join(DATA, "daily_picks")):
    os.makedirs(d, exist_ok=True)
SETTINGS = os.path.join(DATA, "settings.json")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _merge(a, b):
    for k, v in b.items():
        a[k] = _merge(dict(a.get(k) or {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a

def load_config():
    p = os.environ.get("SNIPRIOT_CONFIG", os.path.join(SYSTEM_DIR, "config.json"))
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    if SETTINGS and os.path.exists(SETTINGS):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                cfg = _merge(cfg, json.load(f))
        except Exception:
            pass
    return cfg

def save_settings(changes):
    cur = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as f: cur = json.load(f)
    cur = _merge(cur, changes)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(cur, f, indent=1)
    os.replace(tmp, SETTINGS)
    return cur

def now():
    return time.time()

def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None

def hms(sec):
    sec = int(max(0, sec)); return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"

_log_lock = threading.Lock()
def log(stream, level, msg):
    line = f"{iso(now())} [{level}] {stream or '-'}: {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(os.path.join(DATA, "logs", datetime.date.today().isoformat() + ".log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    try:
        db_exec("INSERT INTO events(ts,stream_id,level,msg) VALUES(?,?,?,?)", (now(), stream, level, msg))
    except Exception:
        pass
    if level in ("ALERT",):
        with _log_lock, open(ALERTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso(now()), "stream": stream, "msg": msg, "sent": False}) + "\n")

_db_lock = threading.RLock()
def db():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

_conn = None
def conn():
    global _conn
    if _conn is None:
        _conn = db()
    return _conn

def db_exec(sql, args=()):
    with _db_lock:
        c = conn(); cur = c.execute(sql, args); c.commit(); return cur

def db_all(sql, args=()):
    with _db_lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]

def db_one(sql, args=()):
    r = db_all(sql, args); return r[0] if r else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY, source_id TEXT, name TEXT, ended_at REAL, url TEXT, video_id TEXT, title TEXT,
  broadcast_start REAL, live_status TEXT, recording INTEGER DEFAULT 0, rec_since REAL,
  earliest_available REAL, bytes_per_s REAL, last_checked REAL, note TEXT);
CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY, stream_id TEXT, path TEXT, wall_start REAL, wall_end REAL,
  duration REAL, source TEXT, aligned TEXT, verified INTEGER, bytes INTEGER, deleted INTEGER DEFAULT 0, rms_path TEXT);
CREATE INDEX IF NOT EXISTS seg_s ON segments(stream_id, wall_start);
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id TEXT, start REAL, end REAL,
  status TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS blocks(id TEXT PRIMARY KEY, stream_id TEXT, idx INTEGER, start REAL, end REAL,
  status TEXT, coverage REAL, clipped_at REAL, n_moments INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS moments(id TEXT PRIMARY KEY, block_id TEXT, stream_id TEXT, t_peak REAL, start REAL, end REAL,
  score_db REAL, source_path TEXT, sheet_path TEXT, meta_path TEXT, created REAL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, params TEXT, status TEXT,
  result TEXT, created REAL, done REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, stream_id TEXT, level TEXT, msg TEXT);
"""
def init_db():
    with _db_lock:
        conn().executescript(SCHEMA); conn().commit()

def kill_tree(proc):
    """Kill a process and everything it started (yt-dlp starts helpers that otherwise keep our pipes open)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            proc.kill()
    except Exception:
        try: proc.kill()
        except Exception: pass

def run(cmd, timeout=None):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=CREATE_NO_WINDOW,
                         encoding="utf-8", errors="replace")
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try: p.communicate(timeout=15)
        except Exception: pass
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)

def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW, **kw)

def probe_duration(cfg, path):
    try:
        r = run([cfg["ffprobe"], "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return None

def free_gb(path=None):
    path = path or DATA_ROOT
    return shutil.disk_usage(path).free / 1e9

def stream_cfg(cfg, tid):
    """tid is '<streamer id>@<video id>' (one timeline per broadcast) or a bare streamer id."""
    sid = tid.split("@")[0]
    # a streamer missing from settings (removed, or the list was wiped) must not stop the whole pipeline
    return next((s for s in cfg["streams"] if s["id"] == sid), {"id": sid, "name": sid, "url": "", "credit": "", "enabled": False})
