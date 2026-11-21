"""Phone notifications for the SnipRiot app (Web Push, free, no app store needed).

The phone app (ui/studio.html installed to the home screen) subscribes once; this thread watches
data/alerts.jsonl and pushes each new alert to every subscribed phone, through Tailscale or the cloud.

Needs: pip install pywebpush   (pulls in py-vapid + cryptography; all free, open source)
Keys:  made on first use in data/push/vapid_private.pem (never share it); public key served at /api/push/key.
iPhone: works once the app is added to the Home Screen (iOS 16.4+). Android Chrome: works in the browser too.
"""
import base64, json, os, time

from common import DATA, log

PUSH = os.path.join(DATA, "push")
SUBS = os.path.join(PUSH, "subscriptions.json")
PRIV = os.path.join(PUSH, "vapid_private.pem")
CONTACT = "mailto:owner@snipriot.local"      # VAPID needs a contact; a placeholder is fine for a private app


def _vapid():
    from py_vapid import Vapid01
    os.makedirs(PUSH, exist_ok=True)
    if not os.path.exists(PRIV):
        v = Vapid01(); v.generate_keys(); v.save_key(PRIV)
    return Vapid01.from_file(PRIV)


def public_key():
    from cryptography.hazmat.primitives import serialization
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _subs():
    try:
        return json.load(open(SUBS, encoding="utf-8"))
    except Exception:
        return []


def subscribe(sub):
    if not sub or not sub.get("endpoint"):
        return
    subs = [s for s in _subs() if s.get("endpoint") != sub["endpoint"]] + [sub]
    os.makedirs(PUSH, exist_ok=True)
    json.dump(subs, open(SUBS, "w", encoding="utf-8"))


def send(title, body, url="/app"):
    from pywebpush import webpush, WebPushException
    keep = []
    for s in _subs():
        try:
            webpush(s, json.dumps({"title": title, "body": body, "url": url}), vapid_private_key=PRIV,
                    vapid_claims={"sub": CONTACT}, ttl=3600)
            keep.append(s)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code not in (404, 410):          # 404/410 = the phone unsubscribed: drop it
                keep.append(s)
        except Exception:
            keep.append(s)
    json.dump(keep, open(SUBS, "w", encoding="utf-8"))


def loop(cfg):
    """Push every new line of alerts.jsonl. Starts at the end of the file, so old alerts aren't re-sent."""
    import studio_api   # puts tools/winlib2 on the path and installs pywebpush once if it's missing
    while not studio_api.ensure("pywebpush", "pywebpush"):
        time.sleep(300)
    path = os.path.join(DATA, "alerts.jsonl")
    pos = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        try:
            if os.path.exists(path) and os.path.getsize(path) < pos:
                pos = 0                          # file was rotated
            if os.path.exists(path) and os.path.getsize(path) > pos:
                with open(path, encoding="utf-8") as f:
                    f.seek(pos); new = f.read(); pos = f.tell()
                for line in new.splitlines():
                    try:
                        a = json.loads(line)
                    except Exception:
                        continue
                    msg = a.get("msg") or a.get("message") or str(a)
                    who = (a.get("stream") or a.get("stream_id") or "SnipRiot").split("@")[0]
                    send(f"SnipRiot · {who}", msg[:180])
        except Exception as e:
            log(None, "WARN", f"phone notifications: {type(e).__name__}: {e}")
        time.sleep(15)
