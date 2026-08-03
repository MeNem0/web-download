# Music Downloader (web)

Tailscale-oriented web UI for downloading Spotify/YouTube playlists as tagged MP3s.

## Docker

```bash
cp .env.example .env
# set TAILSCALE_IP and HOST_DOWNLOAD_DIR
docker compose up -d --build
```

Open `http://<TAILSCALE_IP>:8787`.

## Local (venv)

```bash
pip install -r requirements.txt
python web_server.py
```
