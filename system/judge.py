"""Judge clip candidates with a Gemini model: it watches each 32 s clip with sound.

Runs inside the worker as the 'judge' job (data/job_inbox/*.json, kind 'judge').
GEMINI_API_KEY is read from the environment only; it is never written to disk or logged.
Results go to data/judge/<tag>.json. Clip records and their status are NOT changed.
Transient errors (503/429/5xx, timeouts, dropped connections) are retried with backoff.
Clips that already have a verdict in data/judge/<tag>.json are skipped on re-runs."""
import http.client, json, os, threading, time, urllib.request, urllib.error
from common import DATA, DATA_ROOT, log

API = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-3.8-flash"
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_TRIES = 6

RUBRIC = """You are judging one 32-second clip cut from a live stream, a podcast or a GTA VI moment, for a vertical YouTube Short on a reaction channel.
Watch the picture and listen to the sound, then answer in the JSON schema.

The channel posts reaction content. A clip is only worth posting if it has a first-glance hook: a visual or a line of text in the first 1-2 seconds that stops a scroller and makes people want to comment. A good moment with a slow opening is not enough.

Standards, in order:
1. Hook: the first 2 seconds show a strong visual or say something strong (a shock, a fail, a rage moment, a funny face, a bold claim, an argument starting).
2. Reaction: someone on screen reacts to something visible or audible, and the reaction is the point.
3. Comment bait: the clip makes a viewer want to reply (agree, disagree, take a side, guess what happens next).
4. Payoff: the moment lands inside the 32 seconds.
5. Standalone: someone who has never seen the stream can follow it, and it reads on a phone without chat or overlays.
Weak: shopping, menus, walking, waiting, loading screens, calm dialogue with no reaction, montages with no payoff.
If the clip shows nudity or sexual content, set sexual_content to true and verdict to "cut".

Fields:
- verdict: "keep" (post it), "borderline", or "cut"
- score: integer 1-10 for how strong the first-glance hook and reaction are (10 = best of the best among reaction clips)
- hook_seconds: second the strongest hook starts (0-32)
- hook_text: the text you would put on the first frame, at most 6 words
- hook: the hook in one short sentence
- payoff_seconds: second the main reaction or payoff lands (0-32), or -1 if there is none
- comment_bait: what would make people comment, in a few words, or "none"
- source_type: "live_stream", "podcast", "gta_moment", or "other"
- sexual_content: true if the clip shows nudity or sexual content, otherwise false
- reason: why, in one or two sentences
- title: a short title for a Short, under 60 characters, no quotation marks"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["keep", "borderline", "cut"]},
        "score": {"type": "INTEGER"},
        "hook_seconds": {"type": "NUMBER"},
        "hook_text": {"type": "STRING"},
        "hook": {"type": "STRING"},
        "payoff_seconds": {"type": "NUMBER"},
        "comment_bait": {"type": "STRING"},
        "source_type": {"type": "STRING", "enum": ["live_stream", "podcast", "gta_moment", "other"]},
        "sexual_content": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
        "title": {"type": "STRING"},
    },
    "required": ["verdict", "score", "hook_seconds", "hook_text", "hook", "payoff_seconds",
                 "comment_bait", "source_type", "sexual_content", "reason", "title"],
}

class Transient(RuntimeError):
    """Temporary failure: worth retrying after a pause."""

def _key():
    k = os.environ.get("GEMINI_API_KEY", "")
    if not k:
        raise RuntimeError("GEMINI_API_KEY is not set in the worker's environment")
    return k

def _hdr(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None

def _req(url, data=None, headers=None, method=None):
    h = {"x-goog-api-key": _key()}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=240) as resp:
            status, hd, body = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, hd, body = e.code, dict(e.headers), e.read()
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise Transient(f"network error: {e}")
    if status in RETRY_CODES:
        raise Transient(f"HTTP {status} from Gemini")
    return status, hd, body

def _upload(path):
    size = os.path.getsize(path)
    start = json.dumps({"file": {"display_name": os.path.basename(path)}}).encode()
    st, hd, body = _req(f"{API}/upload/v1beta/files", start, {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": "video/mp4",
        "Content-Type": "application/json"})
    url = _hdr(hd, "X-Goog-Upload-URL")
    if st >= 300 or not url:
        raise RuntimeError(f"upload start failed with HTTP {st}")
    with open(path, "rb") as fh:
        data = fh.read()
    st, hd, body = _req(url, data, {"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    if st >= 300:
        raise RuntimeError(f"upload failed with HTTP {st}")
    f = json.loads(body)["file"]
    for _ in range(120):
        if f.get("state") == "ACTIVE":
            return f
        if f.get("state") == "FAILED":
            raise RuntimeError("Gemini could not process the video")
        time.sleep(3)
        st, hd, body = _req(f"{API}/v1beta/{f['name']}")
        f = json.loads(body)
    raise RuntimeError("video was still processing after 6 minutes")

def _judge_one(model, file_uri):
    payload = {
        "contents": [{"parts": [
            {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
            {"text": RUBRIC}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.2},
    }
    st, hd, body = _req(f"{API}/v1beta/models/{model}:generateContent",
                        json.dumps(payload).encode(), {"Content-Type": "application/json"})
    if st >= 300:
        raise RuntimeError(f"model call failed with HTTP {st}: {body[:200].decode('utf-8', 'replace')}")
    text = json.loads(body)["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)

def _proxy(src, ffmpeg):
    """Small 480p copy for judging: a much smaller upload, so a slow laptop connection does not time out."""
    import subprocess
    out_dir = os.path.join(DATA, "judge_proxy"); os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + ".mp4")
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        cmd = [ffmpeg, "-y", "-v", "error", "-i", src, "-vf", "scale=-2:480", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "64k", "-ac", "1",
               "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("proxy failed: " + r.stderr.decode("utf-8", "replace")[-200:])
    return out

def _judge_clip(model, d, ffmpeg):
    f = _upload(_proxy(os.path.join(DATA_ROOT, d["source"]), ffmpeg))
    try:
        return _judge_one(model, f["uri"])
    finally:
        try:
            _req(f"{API}/v1beta/{f['name']}", method="DELETE")
        except Exception:
            pass

def _with_retry(fn):
    for attempt in range(1, MAX_TRIES + 1):
        try:
            return fn()
        except Transient as e:
            if attempt == MAX_TRIES:
                raise
            wait = 30 * attempt
            log(None, "INFO", f"judge: temporary error ({e}); retry {attempt} in {wait}s")
            time.sleep(wait)

def run(cfg, p):
    model = p.get("model") or DEFAULT_MODEL
    tag = p.get("tag") or time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(DATA, "judge")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{tag}.json")
    ff = cfg.get("ffmpeg") or "ffmpeg"
    ffmpeg = ff[0] if isinstance(ff, list) else ff
    done = {}
    if os.path.exists(out_path):
        try:
            for r in json.load(open(out_path, encoding="utf-8")):
                if r.get("verdict"):
                    done[r["clip"]] = r
        except Exception:
            done = {}
    results = []
    for meta_rel in p.get("clips", []):
        stem = os.path.splitext(os.path.basename(meta_rel))[0]
        if stem in done:
            results.append(done[stem])
            continue
        row = {"clip": stem, "model": model}
        try:
            d = json.load(open(os.path.join(DATA_ROOT, meta_rel), encoding="utf-8"))
            row["title_now"] = d.get("title")
            row["status_now"] = d.get("status")
            row.update(_with_retry(lambda: _judge_clip(model, d, ffmpeg)))
        except Exception as e:
            row["error"] = str(e)[:300]
        results.append(row)
        log(None, "INFO", f"judge {row['clip']}: {row.get('verdict', 'error')} score={row.get('score', '-')}")
        import worklog
        _hook = str(row.get("hook") or row.get("error") or "")[:140]
        worklog.add("judge", row["clip"], f"{row.get('verdict', 'error')} score {row.get('score', '-')} · {_hook}")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1, ensure_ascii=False)
    return {"judged": len(results), "file": out_path, "key_present": bool(os.environ.get("GEMINI_API_KEY"))}

_LOCK = threading.Lock()

def start(cfg, p):
    """Run one judge job in a background thread, so a slow or hung model call cannot stall the worker loop.
    Judge jobs run one at a time. Progress goes to data/judge/<tag>.json and the log."""
    def _go():
        with _LOCK:
            try:
                res = run(cfg, p)
                log(None, "INFO", f"judge finished: {res.get('judged')} clips -> {res.get('file')}")
            except Exception as e:
                log(None, "ERROR", f"judge stopped: {type(e).__name__}: {e}")
    threading.Thread(target=_go, name="judge", daemon=True).start()
    return {"started": True, "tag": p.get("tag"), "clips": len(p.get("clips", []))}
