# Music Downloader (web)

Tailscale-oriented web UI for downloading Spotify/YouTube playlists as tagged MP3s.

## Docker

```bash
cp .env.example .env
# Edit .env: set TAILSCALE_IP and HOST_DOWNLOAD_DIR
docker compose up -d --build
```

Open `http://<TAILSCALE_IP>:8787`.

### Change where files save on the host

1. Edit `HOST_DOWNLOAD_DIR` in `.env` (host path, e.g. `D:/Music`).
2. Recreate so Docker remounts the folder:

```bash
docker compose up -d --force-recreate
```

Files always land in that host folder. Inside the container the path is `/data`.

## Local (venv)

Runs the same app without Docker — useful for developing, or for a machine
where you'd rather not run a container. The Docker image installs `ffmpeg`
and Node.js automatically; running locally, you'll need them yourself:

- **Python 3.11+**
- **ffmpeg** — required, downloads fail without it.
  Windows: `winget install Gyan.FFmpeg`. macOS: `brew install ffmpeg`.
  Linux: `apt install ffmpeg` (or your distro's equivalent).
- **Node.js** — optional. Without it, some yt-dlp player clients that need
  to solve a JS challenge are skipped; downloads still work, just with a
  smaller set of fallback methods.

```bash
python -m venv .venv
.venv\Scripts\activate    # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python web_server.py
```

The startup log (and the web UI itself) will flag it if ffmpeg isn't found.

By default it binds to your Tailscale IP if `tailscale` is installed and
running, otherwise `127.0.0.1` only. Override with `BIND_HOST` (e.g.
`BIND_HOST=0.0.0.0` to reach it from other devices without Tailscale) and
`PORT` if needed — see the top of [web_server.py](web_server.py) for the
full list of env vars.

## Push to GitHub

This folder is already a git repo. To put it on your account:

1. Create a new empty repository on GitHub (no README/license).
2. Point `origin` at it and push:

```bash
git remote remove origin
git remote add origin https://github.com/YOUR_USER/web-download.git
git push -u origin master
```

Or with the GitHub CLI (after `winget install GitHub.cli` and `gh auth login`):

```bash
gh repo create web-download --private --source=. --remote=origin --push
```
