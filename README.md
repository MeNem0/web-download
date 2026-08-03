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

```bash
pip install -r requirements.txt
python web_server.py
```

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
