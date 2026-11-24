"""Live work log for the Studio editor: one JSON line per step (review, track, render, judge).
The Studio polls /api/studio/worklog (studio_api.py) and shows the newest lines first."""
import json, os, threading, time
from common import DATA

PATH = os.path.join(DATA, "worklog.jsonl")
_LOCK = threading.Lock()
KEEP = 2000   # lines kept after rotation

def add(step, clip="", msg=""):
    rec = {"t": round(time.time(), 2), "step": step, "clip": clip, "msg": msg}
    try:
        with _LOCK:
            with open(PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _rotate()
    except Exception:
        pass   # the log must never break a render or a judge run
    return rec

def _rotate():
    if os.path.getsize(PATH) < 400_000:
        return
    with open(PATH, encoding="utf-8") as f:
        lines = f.readlines()[-KEEP:]
    with open(PATH + ".tmp", "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(PATH + ".tmp", PATH)

def recent(n=120):
    try:
        with open(PATH, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[::-1]
