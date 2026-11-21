"""SnipRiot worker: starts the recorders, backfill, clip pipeline and dashboard.
Started automatically at Windows sign-in by START_RECORDER.bat (see SETUP.bat)."""
import os, sys, threading, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import recorder, backfill, pipeline, dashboard, posts, renderer, watcher, watchdog

LOCK = os.path.join(DATA, "worker.lock")

def single_instance():
    # a lock file we keep open; a second copy can't take it while the first runs
    global _lockf
    _lockf = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt; msvcrt.locking(_lockf.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(_lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("SnipRiot worker is already running."); sys.exit(0)

def keep_alive(name, fn, *a):
    def wrap():
        while True:
            try: fn(*a)
            except Exception:
                log(None, "ERROR", f"{name} crashed, restarting in 30s: {traceback.format_exc()[-400:]}")
                time.sleep(30)
    t = threading.Thread(target=wrap, name=name, daemon=True); t.start(); return t

def main():
    single_instance()
    open(os.path.join(DATA, "worker.pid"), "w").write(str(os.getpid()))
    init_db(); posts.init()
    cfg = load_config()
    log(None, "INFO", f"worker starting, {free_gb():.0f} GB free")
    try: recorder.recover_orphans(cfg)
    except Exception as e: log(None, "ERROR", f"orphan recovery: {e}")
    try: recorder.repair_timeline(cfg)
    except Exception as e: log(None, "ERROR", f"timeline repair: {type(e).__name__}: {e}")
    started = set()
    def start_new():  # streamers added to config.json later get a recorder without a restart
        for s in load_config()["streams"]:
            if s["id"] not in started:
                started.add(s["id"]); keep_alive(f"rec-{s['id']}", recorder.Recorder(cfg, s).loop)
    start_new()
    keep_alive("backfill", backfill.loop, cfg)
    keep_alive("replays", backfill.replay_loop, cfg)
    keep_alive("pipeline", pipeline.loop, cfg)
    keep_alive("posts", posts.loop, cfg)
    keep_alive("renderer", renderer.loop, cfg)
    keep_alive("watcher", watcher.loop, cfg)
    keep_alive("watchdog", watchdog.loop, cfg)
    import dropin; keep_alive("dropin", dropin.loop, cfg)
    import notify; keep_alive("notify", notify.loop, cfg)   # phone push alerts
    keep_alive("status", dashboard.write_status_loop, cfg)
    keep_alive("dashboard", dashboard.serve, cfg)
    log(None, "INFO", f"dashboard: http://localhost:{cfg['dashboard_port']}")
    while True:
        open(os.path.join(DATA, "heartbeat"), "w").write(iso(now()))
        try: start_new()
        except Exception as e: log(None, "ERROR", f"config: {e}")
        flag = os.path.join(DATA, "restart_request")
        if os.path.exists(flag):  # Claude drops this file after updating the code; START_RECORDER.bat restarts us in ~30s
            os.remove(flag); log(None, "INFO", "restart requested: stopping recorders and restarting with new code")
            for p in list(recorder.PROCS):
                kill_tree(p)          # the whole tree: yt-dlp's launcher starts the real downloader as a child
            os._exit(3)
        time.sleep(20)

if __name__ == "__main__":
    main()
