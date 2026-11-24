"""Reaction finder (SnipRiot): scores the first 5 seconds of a clip for a reaction on camera and a loud burst
(laughter, shouting). Reads the facecam box from data/cams.json. Face presence uses mediapipe when it is installed,
otherwise a skin-tone fallback. Writes data/reaction_scores.json keyed by clip id; clips already scored are skipped.
Usage: python reaction_score.py --root <SnipRiot folder> [--budget 150] [--hook 5]"""
import argparse, glob, json, os, subprocess, time
import numpy as np
import cv2

try:
    import mediapipe as mp
    _FD = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5)
except Exception:
    _FD = None

def probe(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip().split(",")
    return int(o[0]), int(o[1])

def cam_frames(path, cam, W, H, hook, fps=4):
    x, y, w, h = cam
    cw, ch = int(w * W) // 2 * 2, int(h * H) // 2 * 2
    cx, cy = min(int(x * W) // 2 * 2, W - cw), min(int(y * H) // 2 * 2, H - ch)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", str(hook), "-i", path, "-vf",
                          f"fps={fps},crop={cw}:{ch}:{cx}:{cy},scale=160:120", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    n = len(raw) // (160 * 120 * 3)
    return np.frombuffer(raw[:n * 160 * 120 * 3], np.uint8).reshape(n, 120, 160, 3)

def skin_frac(f):
    ycc = cv2.cvtColor(f, cv2.COLOR_RGB2YCrCb)
    m = (ycc[..., 1] >= 133) & (ycc[..., 1] <= 173) & (ycc[..., 2] >= 77) & (ycc[..., 2] <= 127)
    return float(m.mean())

def audio_burst(path, hook):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", str(hook), "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    win = 3200                                   # 0.2 s windows
    n = len(a) // win
    if n < 3:
        return {"peak_db": None, "burst": 0}
    rms = np.sqrt((a[:n * win].reshape(n, win) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms)
    base = float(np.median(db))
    return {"peak_db": round(float(db.max()), 1), "burst": int((db > base + 10).sum()), "base_db": round(base, 1)}

def face_metrics(frames):
    if _FD is not None:
        pres, cen = [], []
        for f in frames:
            r = _FD.process(f)
            if r.detections:
                bb = r.detections[0].location_data.relative_bounding_box
                pres.append(1.0); cen.append((bb.xmin + bb.width / 2, bb.ymin + bb.height / 2))
            else:
                pres.append(0.0); cen.append(None)
        moves = [np.hypot(cen[i][0] - cen[i - 1][0], cen[i][1] - cen[i - 1][1])
                 for i in range(1, len(cen)) if cen[i] and cen[i - 1]]
        return {"method": "mediapipe", "face": round(float(np.mean(pres)), 3),
                "motion": round(float(np.mean(moves)) if moves else 0.0, 4)}
    skins = [skin_frac(f) for f in frames]
    diffs = [np.abs(cv2.cvtColor(frames[i], cv2.COLOR_RGB2GRAY).astype(np.float32)
                    - cv2.cvtColor(frames[i - 1], cv2.COLOR_RGB2GRAY).astype(np.float32)).mean() / 255.0
             for i in range(1, len(frames))]
    return {"method": "skin", "face": round(min(1.0, float(np.mean(skins)) / 0.25), 3),
            "motion": round(float(np.mean(diffs)) if diffs else 0.0, 4)}

def score(rec):
    f = rec.get("face") or 0.0
    mv = rec.get("motion") or 0.0
    mv_n = min(1.0, mv / (0.05 if rec.get("method") == "mediapipe" else 0.04))
    b = min(1.0, (rec.get("burst") or 0) / 4.0)
    return round(0.4 * f + 0.3 * mv_n + 0.3 * b, 3)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True); ap.add_argument("--budget", type=int, default=150)
    ap.add_argument("--hook", type=float, default=5.0)
    a = ap.parse_args()
    out_path = os.path.join(a.root, "data", "reaction_scores.json")
    scores = json.load(open(out_path)) if os.path.exists(out_path) else {}
    cams = json.load(open(os.path.join(a.root, "data", "cams.json"))) if os.path.exists(os.path.join(a.root, "data", "cams.json")) else {}
    items = []
    for p in glob.glob(os.path.join(a.root, "media", "candidates", "*", "*", "*.json")):
        try:
            m = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(m, dict) or not m.get("source") or m.get("status") not in ("candidate", "final"):
            continue
        cid = m.get("id") or os.path.basename(p)[:-5]
        if cid not in scores:
            items.append((0 if m.get("status") == "final" else 1, cid, m))
    items.sort(key=lambda x: (x[0], x[1]))
    t0, done = time.time(), 0
    for _, cid, m in items:
        if time.time() - t0 > a.budget:
            break
        src = os.path.join(a.root, m["source"])
        base = (m.get("stream_id") or "").split("@")[0]
        try:
            if not os.path.exists(src):
                scores[cid] = {"error": "missing source"}
            else:
                W, H = probe(src)
                cam = (cams.get(base) or {}).get("cam")
                rec = {"status": m.get("status"), "streamer": base}
                rec.update(audio_burst(src, a.hook))
                if cam:
                    rec.update(face_metrics(cam_frames(src, cam, W, H, a.hook)))
                else:
                    rec.update({"method": "none", "face": 0.0, "motion": 0.0})
                rec["score"] = score(rec)
                scores[cid] = rec
        except Exception as e:
            scores[cid] = {"error": str(e)[:120]}
        done += 1
        if done % 25 == 0:
            json.dump(scores, open(out_path + ".tmp", "w"), indent=0); os.replace(out_path + ".tmp", out_path)
    json.dump(scores, open(out_path + ".tmp", "w"), indent=0); os.replace(out_path + ".tmp", out_path)
    print(json.dumps({"scored_now": done, "total_scored": len(scores), "left": len(items) - done}))

if __name__ == "__main__":
    main()
