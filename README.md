# Snip-Studio

Code for SnipRiot: records streams, finds the best moments, cuts clips, and serves the Control Centre and the phone app.

## What is in this repo

- `system/` the worker and dashboard (Python 3.12, Windows)
  - `recorder.py`, `pipeline.py`, `dropin.py` record, download, clip and clean up
  - `dashboard.py` the server for the laptop and the phone (`/` Control Centre, `/app` merged app)
  - `studio_render.py`, `studio_api.py` the Studio renderer and its API
  - `notify.py` phone notifications (Web Push)
  - `ui/` the web app: `index.html` (Control Centre), `studio.html` (merged app), PWA files
  - `config.example.json` the settings template (copy to `config.json` on the laptop and add your streams)
- `scripts/` helper scripts

## What is not in this repo (on purpose)

Stream lists, the phone PIN, push keys, recordings, clips, databases and logs all stay on the laptop in its data folder. The repo is public, so none of those belong here.

## Setup on the laptop

1. Copy `system/config.example.json` to `system/config.json` and fill in your streams.
2. Install the Python packages the worker needs (see `system/setup.ps1`).
3. Start the worker with `START_RECORDER.bat`. Restart after a code change by creating an empty file `data\restart_request`.

## Where things are

- Control Centre: `http://localhost:8765` on the laptop.
- Merged app: `http://localhost:8765/app`.
- Phone: served through Tailscale on the laptop (the PIN is set in the app's settings, not here).
