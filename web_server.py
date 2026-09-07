#!/usr/bin/env python3
"""Tailscale-oriented web UI for Music Downloader.

Bare metal: binds to your Tailscale IP (100.x) by default and rejects
non-Tailscale client IPs.

Docker: set BIND_HOST=0.0.0.0 and REQUIRE_TAILSCALE_CLIENT=0 (compose
defaults). Restrict exposure by publishing the port only on the host's
Tailscale IP.

Run (venv):

    pip install -r requirements.txt
    python web_server.py

Docker:

    docker compose up -d --build

Env vars:
  PORT                      listen port (default 8787)
  BIND_HOST                 override bind address (default: auto Tailscale IP)
  ALLOW_LOCALHOST           allow 127.0.0.1 clients (default 1)
  REQUIRE_TAILSCALE_CLIENT  reject non-tailnet client IPs (default 1; 0 in Docker)
  OUTPUT_DIR                where MP3s are saved (default ./downloads)
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

TRACK_LINE_RE = re.compile(
    r"^\[(\d{3})\] (duplicate, skipping|user skipped|searching|downloading):\s*(.+)$"
)
# Optional [NNN] prefix so completion can target the right track even if
# _active_index was disturbed (e.g. overlapping remediations in older builds).
SAVED_LINE_RE = re.compile(
    r"^ {2}(?:\[(\d{3})\] )?saved(?: with metadata| \(metadata failed:[^)]*\))?:\s*(.+)$"
)
FAILED_LINE_RE = re.compile(
    r"^ {2}(?:\[(\d{3})\] )?failed(?: after \d+ attempt\(s\))?:\s*(.+)$"
)
FAILED_NO_URL = "  failed: no download URL"
FAILED_NO_URL_INDEXED = re.compile(r"^ {2}\[(\d{3})\] failed: no download URL$")
NO_RESULTS_PREFIXES = (
    "  no YouTube search results",
    "  no result within ",
)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from download_playlist import download_strategies, ytdlp_version
from metadata_tags import (
    VARIOUS_ARTISTS,
    read_cover_bytes,
    read_track_metadata,
    update_track_metadata,
)
from music_downloader import (
    DownloadCallbacks,
    Track,
    download_url,
    parse_track_selector,
    remediate_track,
    strategy_ladder,
)

AUDIO_EXTENSIONS = {".mp3"}

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web" / "static"
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_PORT = 8787


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def detect_tailscale_ipv4() -> str | None:
    """Return this machine's Tailscale IPv4, or None if unavailable."""
    exe = shutil.which("tailscale")
    if not exe:
        # Common Windows install path
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        exe = str(candidate) if candidate.is_file() else None
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            addr = ipaddress.ip_address(line)
        except ValueError:
            continue
        if addr.version == 4 and addr in TAILSCALE_CGNAT:
            return str(addr)
    return None


def running_in_docker() -> bool:
    return Path("/.dockerenv").exists() or _env_bool("DOCKER", False)


def resolve_bind_host() -> str:
    override = os.environ.get("BIND_HOST", "").strip()
    if override:
        return override
    if running_in_docker():
        # Containers rarely have the host Tailscale IP; listen on all interfaces
        # and let Compose/host networking control who can reach the port.
        return "0.0.0.0"
    ts = detect_tailscale_ipv4()
    if ts:
        return ts
    # Fail closed for network exposure: loopback only if Tailscale isn't up.
    print(
        "WARNING: Tailscale IPv4 not found. Binding to 127.0.0.1 only.\n"
        "Start Tailscale, then restart this server to listen on your tailnet IP.",
        file=sys.stderr,
    )
    return "127.0.0.1"


def client_ip(request: Request) -> str:
    # Prefer direct peer; ignore X-Forwarded-For (no public proxy in this setup).
    if request.client and request.client.host:
        return request.client.host
    return ""


def is_allowed_client(
    ip: str,
    *,
    allow_localhost: bool,
    require_tailscale_client: bool,
) -> bool:
    if not require_tailscale_client:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr in TAILSCALE_CGNAT:
        return True
    if allow_localhost and addr.is_loopback:
        return True
    return False


def _queue_view(item: dict[str, Any]) -> dict[str, Any]:
    """Queue entry for the UI, minus the bulky approved tracklist.

    The status payload is polled constantly; a reviewed album's plan would add
    kilobytes per queued job for data the queue list never shows.
    """
    options = item.get("options")
    if not isinstance(options, dict) or not options.get("track_plan"):
        return item
    trimmed = dict(item)
    slim = dict(options)
    slim["track_plan"] = None
    slim["planned_tracks"] = len(options["track_plan"])
    trimmed["options"] = slim
    return trimmed


@dataclass
class JobState:
    running: bool = False
    remediating: bool = False
    # True once the current job has reached a terminal state (done, failed,
    # or cancelled) and been filed into history — the "current job" card
    # should disappear at that point rather than keep showing a finished job
    # that already appears in Recent.
    job_finished: bool = False
    status: str = "Ready"
    percent: int = 0
    progress: float = 0.0
    file_percent: int = 0
    completed: int = 0
    total: int = 0
    failed: int = 0
    skipped: int = 0
    url: str = ""
    output_dir: str = ""
    collection: str = ""
    job_id: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    release_type: str = ""
    tracks: list[dict[str, Any]] = field(default_factory=list)
    track_models: dict[int, Track] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    # Failed tracks kept across jobs so the user can fix them later.
    # Each entry keeps the original output_dir, options, and Track model.
    open_fixes: list[dict[str, Any]] = field(default_factory=list)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    cancel_flag: bool = False
    fix_cancel_flag: bool = False
    skip_indices: set[int] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)
    version: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)
    _active_index: int | None = None
    _worker_thread: threading.Thread | None = field(default=None, repr=False)

    def reset_job(self, *, url: str, output_dir: str, options: dict[str, Any] | None = None) -> None:
        self.running = True
        self.job_finished = False
        # Do not clear remediating / open_fixes — fixes run independently of the queue.
        self.cancel_flag = False
        self.status = "Starting…"
        self.percent = 0
        self.progress = 0.0
        self.file_percent = 0
        self.completed = 0
        self.total = 0
        self.failed = 0
        self.skipped = 0
        self.url = url
        self.output_dir = output_dir
        self.collection = ""
        opts = dict(options or {})
        self.job_id = str(opts.get("job_id") or "")
        self.artist = str(opts.get("artist") or "")
        self.album = str(opts.get("album") or "")
        self.album_artist = str(opts.get("album_artist") or "")
        self.release_type = str(opts.get("release_type") or "")
        self.tracks.clear()
        self.track_models.clear()
        self.skip_indices.clear()
        self.options = opts
        self.logs.clear()
        self._active_index = None
        self.bump()

    def set_track_models(self, tracks: list[Track]) -> None:
        with self.lock:
            self.track_models = {t.index: t for t in tracks}
            for model in tracks:
                row = self._track_by_index(model.index)
                row["title"] = model.title
                row["artists"] = model.artists
                if row.get("status") == "pending" and not row.get("detail"):
                    row["detail"] = "Queued"
                row["remediable"] = True
            # Attach models to any open fixes for this job that arrived before models.
            for fix in self.open_fixes:
                if fix.get("job_id") != self.job_id:
                    continue
                idx = fix.get("track_index")
                if idx in self.track_models and fix.get("model") is None:
                    fix["model"] = self.track_models[idx]
                    fix["title"] = self.track_models[idx].title
                    fix["artists"] = self.track_models[idx].artists
            self.bump()

    def _find_open_fix(
        self, *, fix_id: str | None = None, job_id: str | None = None, track_index: int | None = None
    ) -> dict[str, Any] | None:
        if fix_id:
            for fix in self.open_fixes:
                if fix.get("id") == fix_id:
                    return fix
            return None
        if job_id is not None and track_index is not None:
            for fix in self.open_fixes:
                if fix.get("job_id") == job_id and fix.get("track_index") == track_index:
                    return fix
        return None

    def upsert_open_fix(self, track_index: int, detail: str = "") -> dict[str, Any]:
        """Record a failed track with the job's folder + metadata for later remediation."""
        existing = self._find_open_fix(job_id=self.job_id, track_index=track_index)
        row = self._track_by_index(track_index)
        model = self.track_models.get(track_index)
        title = (model.title if model else None) or row.get("title") or f"Track {track_index}"
        artists = (model.artists if model else None) or row.get("artists") or ""
        if existing is not None:
            existing["detail"] = detail or existing.get("detail") or "Failed"
            existing["status"] = "failed"
            existing["title"] = title
            existing["artists"] = artists
            if model is not None:
                existing["model"] = model
            existing["options"] = dict(self.options)
            existing["output_dir"] = self.output_dir
            existing["artist"] = self.artist
            existing["album"] = self.album
            existing["album_artist"] = self.album_artist
            return existing

        fix = {
            "id": str(uuid.uuid4()),
            "job_id": self.job_id,
            "track_index": track_index,
            "title": title,
            "artists": artists,
            "detail": detail or "Failed",
            "status": "failed",
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "output_dir": self.output_dir,
            "options": dict(self.options),
            "model": model,
        }
        self.open_fixes.append(fix)
        return fix

    def remove_open_fix(self, fix_id: str) -> None:
        self.open_fixes = [f for f in self.open_fixes if f.get("id") != fix_id]

    def fixes_snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for fix in self.open_fixes:
            status = fix.get("status") or "failed"
            remediable = (
                status == "failed"
                and not self.remediating
                and fix.get("model") is not None
            )
            out.append(
                {
                    "id": fix.get("id"),
                    "job_id": fix.get("job_id"),
                    "track_index": fix.get("track_index"),
                    "title": fix.get("title"),
                    "artists": fix.get("artists"),
                    "detail": fix.get("detail"),
                    "status": status,
                    "artist": fix.get("artist"),
                    "album": fix.get("album"),
                    "album_artist": fix.get("album_artist"),
                    "output_dir": fix.get("output_dir"),
                    "remediable": remediable,
                }
            )
        return out

    def _current_summary(self) -> dict[str, Any] | None:
        if not self.job_id and not self.running and not self.url:
            return None
        # A finished job already sits in history — don't also leave it
        # parked in the "current job" slot once nothing is actively running.
        if self.job_finished and not self.running and not self.remediating:
            return None
        label = " / ".join(p for p in (self.artist, self.album) if p) or self.collection or self.url
        return {
            "id": self.job_id,
            "url": self.url,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "release_type": self.release_type,
            "output_dir": self.output_dir,
            "label": label,
            "status": "running" if self.running and not self.remediating else (
                "remediating" if self.remediating else self.status
            ),
            "percent": self.percent,
            "completed": self.completed,
            "total": self.total,
            "failed": self.failed,
            "status_text": self.status,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tracks_out = []
            for track in self.tracks:
                item = dict(track)
                item["remediable"] = False
                item["skippable"] = bool(
                    self.running
                    and not self.remediating
                    and item.get("status") == "pending"
                )
                tracks_out.append(item)
            fixes = self.fixes_snapshot()
            return {
                "running": self.running or self.remediating,
                "downloading": self.running,
                "remediating": self.remediating,
                "status": self.status,
                "percent": self.percent,
                "progress": self.progress,
                "file_percent": self.file_percent,
                "completed": self.completed,
                "total": self.total,
                "failed": self.failed,
                "skipped": self.skipped,
                "url": self.url,
                "output_dir": self.output_dir,
                "collection": self.collection,
                "artist": self.artist,
                "album": self.album,
                "album_artist": self.album_artist,
                "release_type": self.release_type,
                "job_id": self.job_id,
                "tracks": tracks_out,
                "fixes": fixes,
                "log": list(self.logs),
                "version": self.version,
                "playlist_track_numbers": bool(
                    self.options.get("playlist_track_numbers")
                ),
                "queue": [_queue_view(item) for item in self.pending],
                "queue_length": len(self.pending),
                "current": self._current_summary(),
                "history": list(self.history),
            }

    def bump(self) -> None:
        self.version += 1
        with self.condition:
            self.condition.notify_all()

    def _track_by_index(self, index: int) -> dict[str, Any]:
        for track in self.tracks:
            if track["index"] == index:
                return track
        track = {
            "index": index,
            "title": f"Track {index}",
            "status": "pending",
            "detail": "",
        }
        self.tracks.append(track)
        self.tracks.sort(key=lambda item: item["index"])
        return track

    def _finish_track(
        self,
        index: int | None,
        status: str,
        detail: str = "",
    ) -> bool:
        """Mark a track terminal. Prefers explicit index; falls back to active."""
        target = index if index is not None else self._active_index
        if target is None:
            return False
        track = self._track_by_index(target)
        if track["status"] in {"done", "skipped", "failed"}:
            if self._active_index == target:
                self._active_index = None
            return False
        track["status"] = status
        if detail:
            track["detail"] = detail
        if self._active_index == target:
            self._active_index = None
        return True

    def _close_stale_active(self, next_index: int | None = None) -> None:
        """If a prior track is still 'active', the downloader has moved on — mark it done."""
        prev = self._active_index
        if prev is None:
            return
        if next_index is not None and prev == next_index:
            return
        track = self._track_by_index(prev)
        if track["status"] == "active":
            track["status"] = "done"
            detail = str(track.get("detail") or "")
            if not detail or detail.startswith(("Downloading", "Searching", "Fixing")):
                track["detail"] = "Saved"
        self._active_index = None

    def finalize_open_tracks(self, *, cancelled: bool = False) -> None:
        """Clear any rows left stuck in 'active' when a job ends."""
        for track in self.tracks:
            if track.get("status") != "active":
                continue
            if cancelled:
                track["status"] = "failed"
                track["detail"] = "Cancelled"
                self.failed += 1
                self.upsert_open_fix(int(track["index"]), "Cancelled")
            else:
                track["status"] = "done"
                detail = str(track.get("detail") or "")
                if not detail or detail.startswith(("Downloading", "Searching", "Fixing")):
                    track["detail"] = "Saved"
        self._active_index = None

    @staticmethod
    def _clean_search_title(raw: str) -> str:
        title = raw
        if " (Spotify:" in title:
            title = title.split(" (Spotify:", 1)[0]
        if title.endswith(" official video"):
            title = title[: -len(" official video")]
        return title.strip() or raw

    def _ingest_line(self, line: str) -> None:
        if line.startswith("Collection:"):
            self.collection = line.split(":", 1)[1].strip()
            return
        if line.startswith("Tracks:"):
            try:
                self.total = max(int(line.split(":", 1)[1].strip()), 0)
            except ValueError:
                pass
            return

        match = TRACK_LINE_RE.match(line)
        if match:
            index = int(match.group(1))
            action = match.group(2)
            raw_title = match.group(3).strip()
            title = (
                self._clean_search_title(raw_title)
                if action == "searching"
                else raw_title
            )
            # Don't reopen a track that already finished (duplicate log / race).
            existing = None
            for row in self.tracks:
                if row["index"] == index:
                    existing = row
                    break
            if existing and existing.get("status") in {"done", "skipped", "failed"}:
                if action in {"duplicate, skipping", "user skipped"}:
                    return
                # Ignore late searching/downloading lines for finished tracks.
                if action in {"searching", "downloading"}:
                    return

            self._close_stale_active(index)
            track = self._track_by_index(index)
            track["title"] = title
            self._active_index = index
            if action == "duplicate, skipping":
                track["status"] = "skipped"
                track["detail"] = "Already downloaded"
                self._active_index = None
            elif action == "user skipped":
                if track["status"] != "skipped":
                    self.skipped += 1
                track["status"] = "skipped"
                track["detail"] = "Skipped by user"
                self._active_index = None
            elif action == "searching":
                track["status"] = "active"
                track["detail"] = "Searching YouTube"
                self.status = f"Searching · {title}"
            else:
                track["status"] = "active"
                track["detail"] = "Downloading"
                self.status = f"Downloading · {title}"
            return

        saved = SAVED_LINE_RE.match(line)
        if saved:
            idx = int(saved.group(1)) if saved.group(1) else None
            detail = saved.group(2).strip()
            self._finish_track(idx, "done", detail)
            return

        indexed_no_url = FAILED_NO_URL_INDEXED.match(line)
        if indexed_no_url or line == FAILED_NO_URL or any(
            line.startswith(p) for p in NO_RESULTS_PREFIXES
        ):
            detail = line.strip()
            idx = int(indexed_no_url.group(1)) if indexed_no_url else self._active_index
            if self._finish_track(idx, "failed", detail):
                self.failed += 1
                if idx is not None:
                    self.upsert_open_fix(idx, detail)
            return

        failed = FAILED_LINE_RE.match(line)
        if failed:
            idx = int(failed.group(1)) if failed.group(1) else self._active_index
            detail = (failed.group(2) or "Failed").strip() or "Failed"
            if self._finish_track(idx, "failed", detail):
                self.failed += 1
                if idx is not None:
                    self.upsert_open_fix(idx, detail)
            return

    def append_log(self, message: str, *, parse: bool = True) -> None:
        """Append log lines. Set parse=False for fix/remediation logs so they
        never update the active download's track list or progress."""
        with self.lock:
            for line in message.splitlines() or [message]:
                self.logs.append(line)
                if parse:
                    self._ingest_line(line)
            self.bump()

    def append_fix_log(self, message: str) -> None:
        """Log remediation output without touching the current download state."""
        with self.lock:
            for line in message.splitlines() or [message]:
                text = line if line.startswith("[fix]") else f"[fix] {line}"
                self.logs.append(text)
            self.bump()

    def sync_fix_track(
        self,
        *,
        fix_job_id: str,
        track_index: int,
        status: str,
        detail: str,
        adjust_failed: int = 0,
    ) -> None:
        """Mirror a fix result onto the current song list only when safe.

        Never mutates the track that the active download is currently working on,
        and never changes _active_index.
        """
        if not fix_job_id or fix_job_id != self.job_id:
            return
        if self._active_index == track_index:
            return
        row = self._track_by_index(track_index)
        # While a download is running, don't flip rows to "active" (two actives).
        if self.running and status == "active":
            return
        prev = row.get("status")
        row["status"] = status
        row["detail"] = detail
        if adjust_failed < 0 and prev == "failed" and status != "failed":
            self.failed = max(0, self.failed + adjust_failed)
        elif adjust_failed > 0 and prev != "failed" and status == "failed":
            self.failed += adjust_failed

    def set_progress(self, completed: int, total: int) -> None:
        with self.lock:
            self.completed = completed
            self.total = max(total, self.total, 1)
            self.file_percent = 0
            overall = self.completed / self.total
            self.progress = max(self.progress, overall)
            self.percent = int(self.progress * 100)
            if self.completed < self.total:
                self.status = f"{self.completed} of {self.total} done"
            self.bump()

    def set_file_progress(self, fraction: float) -> None:
        with self.lock:
            frac = max(0.0, min(1.0, fraction))
            self.file_percent = int(frac * 100)
            overall = (self.completed + frac) / max(self.total, 1)
            self.progress = max(self.progress, overall)
            self.percent = int(self.progress * 100)
            if self._active_index is not None:
                track = self._track_by_index(self._active_index)
                if track["status"] == "active":
                    track["detail"] = f"Downloading {self.file_percent}%"
                    self.status = f"Downloading · {track['title']} · {self.file_percent}%"
            self.bump()


allow_localhost = _env_bool("ALLOW_LOCALHOST", True)
# Docker NAT hides the real client IP, so ACL is off by default in containers.
require_tailscale_client = _env_bool(
    "REQUIRE_TAILSCALE_CLIENT",
    default=not running_in_docker(),
)
default_output_dir = Path(
    os.environ.get("OUTPUT_DIR", str(ROOT / "downloads")),
).expanduser().resolve()
last_output_dir = default_output_dir
# Host path shown in the UI when running under Docker (from compose/.env).
host_download_dir = os.environ.get("HOST_DOWNLOAD_DIR", "").strip()
job = JobState(output_dir=str(default_output_dir))

app = FastAPI(title="Music Downloader", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


FOLDER_NAME_RE = re.compile(r'^[^\x00-\x1f<>:"/\\|?*]+$')


def browse_root() -> Path | None:
    """Limit folder browsing to this tree when set (Docker defaults to /data)."""
    override = os.environ.get("BROWSE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if running_in_docker():
        return default_output_dir.resolve()
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ensure_within_browse_root(path: Path) -> Path:
    root = browse_root()
    resolved = path.resolve()
    if root is None:
        return resolved
    root = root.resolve()
    if resolved != root and not _is_relative_to(resolved, root):
        raise HTTPException(
            status_code=400,
            detail=f"Path must be inside {root}",
        )
    return resolved


def resolve_output_dir(raw: str | None) -> Path:
    """Resolve a save path; create it if needed. In Docker, stay under /data."""
    text = (raw or "").strip()
    path = Path(text).expanduser() if text else default_output_dir
    if not path.is_absolute():
        # Relative paths are created under the browse/output root.
        base = browse_root() or default_output_dir
        path = (base / path).resolve()
    else:
        path = path.resolve()

    path = _ensure_within_browse_root(path) if browse_root() else path

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot use output folder '{path}': {exc}",
        ) from exc
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {path}")
    # Probe write access early so the job fails before downloading.
    probe = path / ".music_downloader_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Folder is not writable: {path} ({exc})",
        ) from exc
    return path


def list_browse_entries(raw: str | None) -> dict[str, Any]:
    """List folders for the web folder picker (host FS, or /data in Docker)."""
    root = browse_root()
    text = (raw or "").strip()

    if root is not None:
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not text:
            path = root
        else:
            path = Path(text).expanduser()
            try:
                path = path.resolve(strict=False)
            except OSError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            path = _ensure_within_browse_root(path)
        if path.exists() and path.is_file():
            path = path.parent
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=404, detail=f"Folder not found: {path}")

        at_root = path.resolve() == root
        parent_path = None if at_root else str(path.parent)
        entries: list[dict[str, str]] = []
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot list folder: {exc}") from exc
        for child in children:
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith("."):
                continue
            try:
                entries.append({"name": name, "path": str(child.resolve())})
            except OSError:
                continue
            if len(entries) >= 400:
                break
        return {
            "path": str(path),
            "parent": parent_path,
            "entries": entries,
            "is_root": at_root,
            "can_create": True,
            "browse_root": str(root),
        }

    # Unrestricted (typical bare-metal Windows): start at drive list.
    if not text:
        if os.name == "nt":
            drives = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = Path(f"{letter}:/")
                if drive.exists():
                    drives.append({"name": f"{letter}:", "path": str(drive.resolve())})
            return {
                "path": "",
                "parent": None,
                "entries": drives,
                "is_root": True,
                "can_create": False,
                "browse_root": None,
            }
        text = str(Path.home())

    path = Path(text).expanduser()
    try:
        path = path.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if path.exists() and path.is_file():
        path = path.parent

    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {path}")

    parent = path.parent
    parent_path = str(parent) if parent != path else None
    if os.name == "nt" and path.drive and path == Path(path.anchor):
        parent_path = ""  # back to drive list

    entries = []
    try:
        children = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list folder: {exc}") from exc

    for child in children:
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("."):
            continue
        try:
            entries.append({"name": name, "path": str(child.resolve())})
        except OSError:
            continue
        if len(entries) >= 400:
            break

    return {
        "path": str(path),
        "parent": parent_path,
        "entries": entries,
        "is_root": False,
        "can_create": True,
        "browse_root": None,
    }


def library_root() -> Path:
    """Root for the library manager (Docker /data, else default downloads)."""
    root = browse_root() or default_output_dir
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_folder_name(name: str) -> str:
    cleaned = (name or "").strip().strip(". ")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned in {".", ".."} or not FOLDER_NAME_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid folder name")
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip(" .")
    return cleaned


def release_path(artist: str, album: str) -> Path:
    """MASTER/ARTIST/ALBUM under the library root."""
    root = library_root()
    path = root / sanitize_folder_name(artist) / sanitize_folder_name(album)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def list_artists() -> list[dict[str, str]]:
    root = library_root()
    artists: list[dict[str, str]] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list artists: {exc}") from exc
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        artists.append({"name": child.name, "path": str(child.resolve())})
    return artists


def create_artist(name: str) -> dict[str, str]:
    cleaned = sanitize_folder_name(name)
    path = library_root() / cleaned
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise HTTPException(status_code=400, detail="Artist already exists") from None
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot create artist: {exc}") from exc
    return {"name": cleaned, "path": str(path.resolve())}


def covers_dir() -> Path:
    path = library_root() / ".covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_cover_bytes(
    cover_path: str | None,
    cover_url: str | None,
) -> tuple[bytes | None, str | None]:
    if cover_path:
        path = Path(cover_path)
        try:
            resolved = path.resolve()
            if not _is_relative_to(resolved, covers_dir()) and not _is_relative_to(
                resolved, library_root()
            ):
                return None, None
            if resolved.is_file():
                data = resolved.read_bytes()
                suffix = resolved.suffix.lower()
                mime = {
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                }.get(suffix, "image/jpeg")
                return data, mime
        except OSError:
            pass
    if cover_url:
        from metadata_tags import _fetch_cover

        cover = _fetch_cover([cover_url])
        if cover:
            return cover
    return None, None


def _resolve_library_path(raw: str | None, *, must_exist: bool = True) -> Path:
    root = library_root()
    text = (raw or "").strip()
    if not text:
        path = root
    else:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve(strict=False)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if path != root and not _is_relative_to(path, root):
        raise HTTPException(status_code=400, detail=f"Path must be inside {root}")
    if must_exist and not path.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    return path


def list_library_entries(raw: str | None) -> dict[str, Any]:
    root = library_root()
    path = _resolve_library_path(raw, must_exist=True)
    if path.is_file():
        path = path.parent

    at_root = path.resolve() == root
    parent_path = None if at_root else str(path.parent)
    folders: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list folder: {exc}") from exc

    for child in children:
        name = child.name
        if name.startswith("."):
            continue
        try:
            resolved = str(child.resolve())
        except OSError:
            continue
        if child.is_dir():
            folders.append({"name": name, "path": resolved, "type": "dir"})
        elif child.is_file() and child.suffix.lower() in AUDIO_EXTENSIONS:
            meta = read_track_metadata(child)
            files.append(
                {
                    "name": name,
                    "path": resolved,
                    "type": "file",
                    "size": child.stat().st_size,
                    "title": meta.get("title") or child.stem,
                    "artists": meta.get("artists") or "",
                    "album": meta.get("album") or "",
                    "has_cover": bool(meta.get("has_cover")),
                }
            )
        if len(folders) + len(files) >= 800:
            break

    return {
        "root": str(root),
        "path": str(path),
        "parent": parent_path,
        "is_root": at_root,
        "folders": folders,
        "files": files,
    }


def search_library(
    query: str,
    raw_path: str | None = None,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Recursive MP3 search by filename, path, title, artists, and album."""
    q = (query or "").strip().lower()
    if not q:
        raise HTTPException(status_code=400, detail="Search query is required")
    tokens = [t for t in re.split(r"\s+", q) if t]
    if not tokens:
        raise HTTPException(status_code=400, detail="Search query is required")

    root = library_root()
    base = _resolve_library_path(raw_path, must_exist=True)
    if base.is_file():
        base = base.parent

    def hay_matches(text: str) -> bool:
        lowered = text.lower()
        return all(token in lowered for token in tokens)

    files: list[dict[str, Any]] = []
    folders: list[dict[str, Any]] = []
    scanned = 0
    max_scan = 8_000

    try:
        walker = base.rglob("*")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot search: {exc}") from exc

    for child in walker:
        if scanned >= max_scan or (len(files) + len(folders)) >= limit:
            break
        name = child.name
        if name.startswith("."):
            continue
        try:
            rel_parts = child.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            resolved = str(child.resolve())
        except OSError:
            continue

        rel = "/".join(rel_parts)
        if child.is_dir():
            if hay_matches(name) or hay_matches(rel):
                folders.append(
                    {
                        "name": name,
                        "path": resolved,
                        "type": "dir",
                        "rel": rel,
                    }
                )
            continue

        if not child.is_file() or child.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        scanned += 1
        matched = hay_matches(name) or hay_matches(rel)
        meta: dict[str, Any] | None = None
        if not matched:
            meta = read_track_metadata(child)
            matched = hay_matches(
                " ".join(
                    [
                        str(meta.get("title") or ""),
                        str(meta.get("artists") or ""),
                        str(meta.get("album") or ""),
                    ]
                )
            )
        if not matched:
            continue
        if meta is None:
            meta = read_track_metadata(child)
        files.append(
            {
                "name": name,
                "path": resolved,
                "type": "file",
                "size": child.stat().st_size,
                "title": meta.get("title") or child.stem,
                "artists": meta.get("artists") or "",
                "album": meta.get("album") or "",
                "has_cover": bool(meta.get("has_cover")),
                "rel": rel,
            }
        )

    folders.sort(key=lambda item: str(item.get("rel") or item.get("name") or "").lower())
    files.sort(key=lambda item: str(item.get("rel") or item.get("name") or "").lower())
    return {
        "root": str(root),
        "path": str(base),
        "query": query.strip(),
        "folders": folders,
        "files": files,
        "scanned": scanned,
        "truncated": scanned >= max_scan or (len(files) + len(folders)) >= limit,
    }


def rename_library_entry(raw_path: str, new_name: str) -> dict[str, Any]:
    cleaned = (new_name or "").strip().strip(". ")
    if not cleaned or cleaned in {".", ".."} or not FOLDER_NAME_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid name")
    if "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid name")

    src = _resolve_library_path(raw_path, must_exist=True)
    root = library_root()
    if src.resolve() == root:
        raise HTTPException(status_code=400, detail="Cannot rename the library root")

    # Keep extension for audio files if the user omitted it.
    if src.is_file() and src.suffix.lower() in AUDIO_EXTENSIONS:
        if Path(cleaned).suffix.lower() not in AUDIO_EXTENSIONS:
            cleaned = f"{cleaned}{src.suffix}"

    dest = (src.parent / cleaned).resolve()
    if dest != root and not _is_relative_to(dest, root):
        raise HTTPException(status_code=400, detail="Invalid destination")
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"Already exists: {cleaned}")
    try:
        src.rename(dest)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Rename failed: {exc}") from exc
    return {"path": str(dest), "name": dest.name, "type": "dir" if dest.is_dir() else "file"}


def delete_library_entry(raw_path: str) -> dict[str, Any]:
    target = _resolve_library_path(raw_path, must_exist=True)
    root = library_root()
    if target.resolve() == root:
        raise HTTPException(status_code=400, detail="Cannot delete the library root")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Delete failed: {exc}") from exc
    return {"ok": True, "deleted": str(target)}


def create_browse_folder(parent: str, name: str) -> dict[str, Any]:
    """Create a subfolder under parent (within browse root when restricted)."""
    cleaned = (name or "").strip().strip(". ")
    if not cleaned or cleaned in {".", ".."} or not FOLDER_NAME_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid folder name")
    if "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid folder name")

    parent_text = (parent or "").strip()
    root = browse_root()
    if not parent_text:
        if root is None:
            raise HTTPException(status_code=400, detail="Choose a parent folder first")
        parent_path = root
    else:
        parent_path = Path(parent_text).expanduser()

    try:
        parent_path = parent_path.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if root is not None:
        parent_path = _ensure_within_browse_root(parent_path)
    if not parent_path.exists() or not parent_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Parent folder not found: {parent_path}")

    new_path = (parent_path / cleaned).resolve()
    if root is not None:
        new_path = _ensure_within_browse_root(new_path)

    try:
        new_path.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=f"Already exists: {cleaned}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot create folder: {exc}") from exc

    return {
        "path": str(new_path),
        "parent": str(parent_path),
        "name": cleaned,
    }


@app.middleware("http")
async def tailscale_only(request: Request, call_next):
    ip = client_ip(request)
    if not is_allowed_client(
        ip,
        allow_localhost=allow_localhost,
        require_tailscale_client=require_tailscale_client,
    ):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Forbidden: only Tailscale (and optionally localhost) clients are allowed.",
                "client": ip,
            },
        )
    return await call_next(request)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1)
    # Blank artist is only valid for compilations, which default to Various Artists.
    artist: str = Field(default="", max_length=120)
    album: str = Field(min_length=1, max_length=120)
    album_artist: str = ""
    release_type: str = "album"  # album | single | compilation
    cover_url: str | None = None
    cover_path: str | None = None
    force: bool = False
    embed_metadata: bool = True
    playlist_track_numbers: bool = True
    debug: bool = False
    # Phrases to delete from every track title, e.g. "official music video".
    strip_terms: str = Field(default="", max_length=500)
    verify_artists: bool = False
    limit: int | None = Field(default=None, ge=1, le=10_000)
    # e.g. "1-10", "!3-5", "1-20 !3-5,!8-10"
    tracks: str | None = Field(default=None, max_length=500)
    browser: str | None = None
    # Songs the user approved in the review step; overrides re-reading the link.
    track_plan: list[dict[str, Any]] | None = Field(default=None, max_length=1000)


class ArtistCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class QueueRemoveBody(BaseModel):
    id: str = Field(min_length=1)


class RemediateRequest(BaseModel):
    fix_id: str | None = None
    track_index: int | None = Field(default=None, ge=1)
    action: str = Field(min_length=1)  # retry | loose | paste_url | skip
    youtube_url: str | None = None
    browser: str | None = None


class TrackSkipBody(BaseModel):
    track_index: int = Field(ge=1)


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    snap = job.snapshot()
    if not snap.get("output_dir"):
        snap["output_dir"] = str(last_output_dir)
    snap["default_output_dir"] = str(default_output_dir)
    snap["host_download_dir"] = host_download_dir or None
    root = browse_root()
    snap["browse_root"] = str(root) if root else None
    snap["docker"] = running_in_docker()
    snap["bind_hint"] = os.environ.get("_ACTIVE_BIND", "")
    snap["ytdlp_version"] = ytdlp_version()
    snap["download_methods"] = [s.name for s in strategy_ladder()]
    return snap


@app.get("/api/system/ytdlp")
async def ytdlp_info() -> dict[str, Any]:
    return {
        "version": ytdlp_version(),
        "methods": [
            {"name": s.name, "clients": list(s.player_clients or []), "note": s.note}
            for s in download_strategies()
        ],
        "active_methods": [s.name for s in strategy_ladder()],
    }


@app.post("/api/system/ytdlp/update")
async def ytdlp_update() -> dict[str, Any]:
    """Upgrade yt-dlp in place.

    YouTube breaks extraction regularly; updating is usually the real fix and
    should not require rebuilding the container image.
    """
    before = ytdlp_version()

    def _pip(extra: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--upgrade",
                *extra,
                "yt-dlp[default,curl-cffi]",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

    def _run() -> subprocess.CompletedProcess[str]:
        proc = _pip([])
        # In Docker the app runs as a non-root user that cannot write to the
        # system site-packages; retry into the per-user location.
        if proc.returncode != 0:
            blob = f"{proc.stderr or ''}{proc.stdout or ''}".lower()
            if "permission denied" in blob or "externally-managed" in blob:
                return _pip(["--user"])
        return proc

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="yt-dlp update timed out") from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Update failed: {exc}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"pip exited {proc.returncode}"
        raise HTTPException(status_code=500, detail=f"Update failed: {detail}")

    after = _reimport_ytdlp_version()
    job.append_log(
        f"yt-dlp updated: {before} → {after}"
        + ("" if after != before else " (already current)")
    )
    return {
        "ok": True,
        "before": before,
        "after": after,
        "restart_required": after != before,
    }


def _reimport_ytdlp_version() -> str:
    """Read the on-disk yt-dlp version after an upgrade.

    The running process keeps the old module loaded, so read the installed
    version in a subprocess rather than trusting the import cache.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        return out or ytdlp_version()
    except Exception:  # noqa: BLE001
        return ytdlp_version()


@app.get("/api/browse")
async def browse(path: str | None = None) -> dict[str, Any]:
    return list_browse_entries(path)


class MkdirRequest(BaseModel):
    parent: str = ""
    name: str = Field(min_length=1, max_length=180)


@app.post("/api/mkdir")
async def mkdir(body: MkdirRequest) -> dict[str, Any]:
    return create_browse_folder(body.parent, body.name)


@app.get("/api/library")
async def library_list(path: str | None = None) -> dict[str, Any]:
    # Reads one ID3 tag per track in the folder; off the event loop so a big
    # folder can't stall the download progress stream for other clients.
    return await asyncio.to_thread(list_library_entries, path)


@app.get("/api/library/search")
async def library_search(
    q: str = "",
    path: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    capped = max(1, min(int(limit or 200), 500))
    # Walks up to 8,000 files reading tags — same reasoning as library_list.
    return await asyncio.to_thread(search_library, q, path, limit=capped)


@app.get("/api/library/artists")
async def library_artists() -> dict[str, Any]:
    return {"root": str(library_root()), "artists": list_artists()}


@app.post("/api/library/artists")
async def library_artists_create(body: ArtistCreateBody) -> dict[str, Any]:
    return create_artist(body.name)


def _store_cover_bytes(data: bytes, mime: str | None = None) -> dict[str, Any]:
    if not data:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Cover image too large (max 12MB)")
    kind = (mime or "image/jpeg").split(";", 1)[0].lower()
    if kind == "image/png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        ext, kind = ".png", "image/png"
    elif kind == "image/webp" or data[:4] == b"RIFF":
        ext, kind = ".webp", "image/webp"
    else:
        ext, kind = ".jpg", "image/jpeg"
    dest = covers_dir() / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(data)
    resolved = str(dest.resolve())
    return {
        "path": resolved,
        "mime": kind,
        "preview_url": f"/api/download/cover/file?path={quote(resolved)}",
    }


@app.post("/api/download/cover")
async def download_cover_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    return _store_cover_bytes(data, file.content_type)


class CoverUrlBody(BaseModel):
    url: str = Field(min_length=1)


@app.post("/api/download/cover/from-url")
async def download_cover_from_url(body: CoverUrlBody) -> dict[str, Any]:
    from metadata_tags import _fetch_cover

    cover = _fetch_cover([(body.url or "").strip()])
    if not cover:
        raise HTTPException(status_code=400, detail="Could not download cover from URL")
    data, mime = cover
    return _store_cover_bytes(data, mime)


@app.get("/api/download/cover/file")
async def download_cover_file(path: str) -> Response:
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Invalid cover path") from exc
    if not _is_relative_to(target, covers_dir()) or not target.is_file():
        raise HTTPException(status_code=404, detail="Cover not found")
    suffix = target.suffix.lower()
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "image/jpeg")
    return Response(content=target.read_bytes(), media_type=mime)


def _upgrade_artwork_url(url: str) -> str:
    """Prefer a larger iTunes/Apple artwork size when available."""
    text = (url or "").strip()
    if not text:
        return ""
    return re.sub(r"\d+x\d+bb", "600x600bb", text, count=1)


@app.get("/api/download/cover/search")
async def download_cover_search(
    q: str = "",
    entity: str = "album",
    limit: int = 8,
) -> dict[str, Any]:
    """Search Apple Music / iTunes for album or song artwork (no API key)."""
    import httpx

    query = (q or "").strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Enter at least 2 characters to search")
    kind = (entity or "album").strip().lower()
    if kind not in {"album", "song", "all"}:
        kind = "album"
    cap = max(1, min(int(limit or 8), 20))
    itunes_entity = {"album": "album", "song": "song", "all": "album,song"}[kind]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://itunes.apple.com/search",
                params={
                    "term": query,
                    "media": "music",
                    "entity": itunes_entity,
                    "limit": cap,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cover search failed: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Cover search returned invalid JSON") from exc

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        artwork = _upgrade_artwork_url(
            str(item.get("artworkUrl100") or item.get("artworkUrl60") or "")
        )
        if not artwork:
            continue
        wrapper = str(item.get("wrapperType") or "")
        item_kind = str(item.get("kind") or "")
        if wrapper == "collection" or item.get("collectionType"):
            result_kind = "album"
            title = str(item.get("collectionName") or "").strip()
            album = title
        elif item_kind == "song" or wrapper == "track":
            result_kind = "song"
            title = str(item.get("trackName") or "").strip()
            album = str(item.get("collectionName") or title).strip()
        else:
            continue
        if not title:
            continue
        artist = str(item.get("artistName") or "").strip()
        dedupe = f"{result_kind}|{artist.casefold()}|{album.casefold()}|{artwork}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        year = ""
        released = str(item.get("releaseDate") or "")
        if len(released) >= 4 and released[:4].isdigit():
            year = released[:4]
        results.append(
            {
                "id": str(item.get("collectionId") or item.get("trackId") or len(results)),
                "kind": result_kind,
                "title": title,
                "album": album,
                "artist": artist,
                "year": year,
                "artwork_url": artwork,
            }
        )
        if len(results) >= cap:
            break

    return {"query": query, "entity": kind, "results": results}


@app.get("/api/library/meta")
async def library_meta(path: str) -> dict[str, Any]:
    target = _resolve_library_path(path, must_exist=True)
    if not target.is_file() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an MP3 file")
    meta = read_track_metadata(target)
    return {
        "path": str(target),
        "name": target.name,
        **meta,
        "cover_url": f"/api/library/cover?path={quote(str(target))}" if meta.get("has_cover") else None,
    }


@app.get("/api/library/cover")
async def library_cover(path: str) -> Response:
    target = _resolve_library_path(path, must_exist=True)
    if not target.is_file() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an MP3 file")
    cover = read_cover_bytes(target)
    if not cover:
        raise HTTPException(status_code=404, detail="No cover art")
    data, mime = cover
    return Response(content=data, media_type=mime)


class LibraryMetaUpdate(BaseModel):
    path: str
    title: str = ""
    artists: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: str = ""
    compilation: bool | None = None
    cover_url: str | None = None
    remove_cover: bool = False


class LibraryMetaBulkUpdate(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    title: str | None = None
    artists: str | None = None
    album: str | None = None
    album_artist: str | None = None
    track_number: str | None = None
    compilation: bool | None = None
    number_sequentially: bool = False
    cover_url: str | None = None
    remove_cover: bool = False


@app.put("/api/library/meta")
async def library_meta_update(body: LibraryMetaUpdate) -> dict[str, Any]:
    target = _resolve_library_path(body.path, must_exist=True)
    if not target.is_file() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an MP3 file")
    try:
        meta = update_track_metadata(
            target,
            title=body.title,
            artists=body.artists,
            album=body.album,
            album_artist=body.album_artist,
            track_number=body.track_number,
            compilation=body.compilation,
            cover_urls=[body.cover_url] if body.cover_url else None,
            remove_cover=body.remove_cover,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not update tags: {exc}") from exc
    return {
        "path": str(target),
        "name": target.name,
        **meta,
        "cover_url": f"/api/library/cover?path={quote(str(target))}" if meta.get("has_cover") else None,
    }


@app.put("/api/library/meta/bulk")
async def library_meta_bulk_update(body: LibraryMetaBulkUpdate) -> dict[str, Any]:
    """Apply shared metadata to many MP3s. Omitted/blank text fields are left unchanged."""
    updated = 0
    errors: list[str] = []
    for index, raw in enumerate(body.paths, start=1):
        try:
            target = _resolve_library_path(raw, must_exist=True)
            if not target.is_file() or target.suffix.lower() not in AUDIO_EXTENSIONS:
                raise ValueError("Not an MP3 file")
            track_number: str | int | None
            if body.number_sequentially:
                track_number = index
            elif body.track_number is not None and str(body.track_number).strip() != "":
                track_number = body.track_number
            else:
                track_number = None

            def _text(value: str | None) -> str | None:
                if value is None:
                    return None
                text = value.strip()
                return text if text else None

            update_track_metadata(
                target,
                title=_text(body.title),
                artists=_text(body.artists),
                album=_text(body.album),
                album_artist=_text(body.album_artist),
                track_number=track_number,
                compilation=body.compilation,
                cover_urls=[body.cover_url] if body.cover_url else None,
                remove_cover=body.remove_cover,
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{Path(raw).name}: {exc}")
    if not updated and errors:
        raise HTTPException(status_code=400, detail=errors[0])
    return {"ok": True, "updated": updated, "errors": errors}


@app.post("/api/library/cover")
async def library_cover_upload(
    path: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    target = _resolve_library_path(path, must_exist=True)
    if not target.is_file() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an MP3 file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Cover image too large (max 12MB)")
    mime = (file.content_type or "image/jpeg").split(";", 1)[0]
    try:
        meta = update_track_metadata(
            target,
            cover_bytes=data,
            cover_mime=mime,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not set cover: {exc}") from exc
    return {
        "path": str(target),
        "name": target.name,
        **meta,
        "cover_url": f"/api/library/cover?path={quote(str(target))}" if meta.get("has_cover") else None,
    }


class LibraryPathBody(BaseModel):
    path: str


class LibraryRenameBody(BaseModel):
    path: str
    new_name: str = Field(min_length=1, max_length=180)


@app.post("/api/library/rename")
async def library_rename(body: LibraryRenameBody) -> dict[str, Any]:
    return rename_library_entry(body.path, body.new_name)


@app.post("/api/library/delete")
async def library_delete(body: LibraryPathBody) -> dict[str, Any]:
    return delete_library_entry(body.path)


@app.post("/api/library/mkdir")
async def library_mkdir(body: MkdirRequest) -> dict[str, Any]:
    parent = body.parent.strip() or str(library_root())
    # Ensure mkdir stays inside the library tree even on bare metal.
    _resolve_library_path(parent, must_exist=True)
    return create_browse_folder(parent, body.name)


@app.get("/api/events")
async def events(request: Request):
    """Long-poll style SSE for live progress."""
    from starlette.responses import StreamingResponse

    async def gen():
        last = -1
        while True:
            if await request.is_disconnected():
                break
            snap = job.snapshot()
            if snap["version"] != last:
                last = snap["version"]
                import json

                yield f"data: {json.dumps(snap)}\n\n"
            else:
                # Wait briefly for updates without busy-spinning.
                await asyncio.to_thread(_wait_for_bump, 1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _wait_for_bump(timeout: float) -> None:
    with job.condition:
        job.condition.wait(timeout=timeout)


def _is_compilation(options: dict[str, Any]) -> bool:
    """Whether a job is a Various Artists release.

    Falls back to release_type so jobs queued before the flag existed still work.
    """
    if "compilation" in options:
        return bool(options.get("compilation"))
    return str(options.get("release_type") or "").strip().lower() == "compilation"


def _queue_item_from_body(body: DownloadRequest) -> dict[str, Any]:
    release = (body.release_type or "album").strip().lower()
    if release not in {"album", "single", "compilation"}:
        release = "album"
    compilation = release == "compilation"
    # Compilations live under their album artist so Jellyfin keeps the album
    # whole instead of splitting it across one folder per performer.
    artist = sanitize_folder_name(body.artist or (VARIOUS_ARTISTS if compilation else ""))
    album = sanitize_folder_name(body.album)
    # Metadata album artist defaults to the chosen Link artist when left blank.
    album_artist = (body.album_artist or "").strip() or artist
    dest = release_path(artist, album)
    browser = body.browser if body.browser and body.browser != "none" else None
    job_id = uuid.uuid4().hex[:12]
    label = f"{artist} / {album}"
    return {
        "id": job_id,
        "url": body.url.strip(),
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "release_type": release,
        "output_dir": str(dest),
        "label": label,
        "status": "pending",
        "percent": 0,
        "status_text": "Queued",
        "cover_url": (body.cover_url or "").strip() or None,
        "cover_path": (body.cover_path or "").strip() or None,
        "options": {
            "job_id": job_id,
            "artist": artist,
            "album": album,
            "album_artist": album_artist,
            "release_type": release,
            "compilation": compilation,
            "strip_terms": (body.strip_terms or "").strip(),
            # Online lookup only helps where per-track artists are uncertain.
            "verify_artists": bool(body.verify_artists) and compilation,
            "force": body.force,
            "embed_metadata": body.embed_metadata,
            "playlist_track_numbers": body.playlist_track_numbers,
            "debug": body.debug,
            "limit": body.limit,
            "tracks": (body.tracks or "").strip() or None,
            "track_plan": body.track_plan or None,
            "browser": browser,
            "cover_url": (body.cover_url or "").strip() or None,
            "cover_path": (body.cover_path or "").strip() or None,
        },
    }


def _ensure_queue_worker() -> None:
    """Start the next queued download. Failures / remediations never block the queue."""
    with job.lock:
        # Recover if a prior worker thread died without clearing ``running``.
        worker = job._worker_thread
        if job.running and worker is not None and not worker.is_alive():
            job.running = False
            job.status = "Recovered stuck worker — resuming queue"
            job.append_log("Queue worker was stuck; recovered and continuing.")
            job.bump()
        if job.running:
            return
        if not job.pending:
            job._worker_thread = None
            return
        item = job.pending.pop(0)
        dest = Path(item["output_dir"])
        options = dict(item["options"])
        url = item["url"]
        job.reset_job(url=url, output_dir=str(dest), options=options)

    thread = threading.Thread(
        target=_run_download,
        kwargs={"dest": dest, "options": options, "url": url},
        daemon=True,
        name=f"download-{options.get('job_id') or 'job'}",
    )
    with job.lock:
        job._worker_thread = thread
    thread.start()


def _resolve_fix(body: RemediateRequest) -> dict[str, Any]:
    """Locate an open fix by id, or by current-job track index."""
    if body.fix_id:
        fix = job._find_open_fix(fix_id=body.fix_id)
        if fix is None:
            raise HTTPException(status_code=404, detail="Fix item not found")
        return fix
    if body.track_index is not None:
        fix = job._find_open_fix(job_id=job.job_id, track_index=body.track_index)
        if fix is None:
            # Lazily create from current job state (still on the same album).
            row = job._track_by_index(body.track_index)
            if row.get("status") not in {"failed", "skipped"}:
                raise HTTPException(
                    status_code=400,
                    detail="Only failed or skipped tracks can be remediated",
                )
            fix = job.upsert_open_fix(
                body.track_index, str(row.get("detail") or "Failed")
            )
        return fix
    raise HTTPException(status_code=400, detail="fix_id or track_index is required")


class AlbumMatchRequest(BaseModel):
    url: str = Field(min_length=1)
    album: str = Field(default="", max_length=200)
    artist: str = Field(default="", max_length=200)
    release_type: str = "album"
    collection_id: str = Field(default="", max_length=64)
    strip_terms: str = Field(default="", max_length=500)
    limit: int | None = Field(default=None, ge=1, le=10_000)
    tracks: str | None = Field(default=None, max_length=500)
    browser: str | None = None


@app.post("/api/album/match")
async def album_match(body: AlbumMatchRequest) -> dict[str, Any]:
    """Line a playlist up against the catalogued album, without queueing.

    Runs entirely outside the job state so a review can be done while another
    download is in flight.
    """
    import album_match as matcher
    from music_downloader import fetch_source_tracks, track_as_dict

    compilation = (body.release_type or "").strip().lower() == "compilation"
    browser = body.browser if body.browser and body.browser != "none" else None

    def _read_source() -> tuple[str, list[dict[str, Any]]]:
        name, tracks = fetch_source_tracks(
            body.url.strip(),
            browser=browser,
            limit=body.limit,
            tracks_spec=(body.tracks or "").strip() or None,
            strip_terms=body.strip_terms,
            compilation=compilation,
        )
        return name, [track_as_dict(t) for t in tracks]

    try:
        collection_name, sources = await asyncio.to_thread(_read_source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read link: {exc}") from exc

    album_name = (body.album or "").strip() or collection_name

    def _read_album() -> Any:
        return matcher.find_album(
            album_name,
            body.artist,
            collection_id=(body.collection_id or "").strip(),
            various=compilation,
        )

    album = None
    album_error = ""
    try:
        album = await asyncio.to_thread(_read_album)
    except Exception as exc:  # noqa: BLE001 - matching is optional
        album_error = str(exc)

    if album is None:
        # No catalogue entry: still let the user review and approve the playlist.
        rows = [
            {
                "kind": "extra",
                "number": idx,
                "album_track": None,
                "source": source,
                "score": 0.0,
                "include": True,
            }
            for idx, source in enumerate(sources, start=1)
        ]
        return {
            "collection": collection_name,
            "album": None,
            "album_error": album_error or "No catalogue match for that album name",
            "sources": sources,
            "rows": rows,
            "summary": {
                "matched": 0,
                "missing": 0,
                "extra": len(sources),
                "source_count": len(sources),
            },
        }

    rows = matcher.match_playlist(album, sources)
    matched = sum(1 for r in rows if r["kind"] == "matched")
    missing = sum(1 for r in rows if r["kind"] == "missing")
    extra = sum(1 for r in rows if r["kind"] == "extra")
    return {
        "collection": collection_name,
        "album": album.as_dict(),
        "album_error": "",
        # Every playlist song, so a row can be re-pointed at the right one.
        "sources": sources,
        "rows": rows,
        "summary": {
            "matched": matched,
            "missing": missing,
            "extra": extra,
            "source_count": len(sources),
        },
    }


@app.get("/api/album/search")
async def album_search(q: str = "", limit: int = 8) -> dict[str, Any]:
    """Album candidates so the user can correct a wrong automatic match."""
    import album_match as matcher

    query = (q or "").strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Enter at least 2 characters")
    try:
        found = await asyncio.to_thread(matcher.search_albums, query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Album search failed: {exc}") from exc
    return {
        "query": query,
        "results": [
            {
                "id": a.id,
                "name": a.name,
                "artist": a.artist,
                "year": a.year,
                "artwork_url": a.artwork_url,
                "track_count": a.track_count,
                "various_artists": a.is_various_artists,
            }
            for a in found
        ],
    }


@app.post("/api/download")
async def start_download(body: DownloadRequest) -> dict[str, Any]:
    global last_output_dir
    tracks_spec = (body.tracks or "").strip() or None
    if tracks_spec:
        try:
            parse_track_selector(tracks_spec)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = _queue_item_from_body(body)
    last_output_dir = Path(item["output_dir"])
    with job.lock:
        job.pending.append(item)
        job.bump()
    _ensure_queue_worker()
    return {"ok": True, "queued": item, "status": job.snapshot()}


@app.post("/api/queue/remove")
async def queue_remove(body: QueueRemoveBody) -> dict[str, Any]:
    with job.lock:
        before = len(job.pending)
        job.pending = [item for item in job.pending if item.get("id") != body.id]
        if len(job.pending) == before:
            raise HTTPException(status_code=404, detail="Queued job not found")
        job.bump()
    return {"ok": True, "status": job.snapshot()}


@app.post("/api/remediate")
async def start_remediate(body: RemediateRequest) -> dict[str, Any]:
    action = body.action.strip().lower()
    if action not in {"retry", "loose", "paste_url", "skip"}:
        raise HTTPException(
            status_code=400,
            detail="action must be retry, loose, paste_url, or skip",
        )

    skip_title: str | None = None
    launch: dict[str, Any] | None = None

    with job.lock:
        if action != "skip" and job.remediating:
            raise HTTPException(
                status_code=409,
                detail="Another fix is already in progress",
            )
        fix = _resolve_fix(body)
        if fix.get("status") not in {"failed", "skipped"}:
            raise HTTPException(
                status_code=400,
                detail="Only failed or skipped tracks can be remediated",
            )
        model = fix.get("model")
        if model is None and action != "skip":
            raise HTTPException(
                status_code=404,
                detail="Track metadata unavailable for this fix.",
            )

        if action == "skip":
            fix_id = str(fix["id"])
            skip_title = str(fix.get("title") or "track")
            job.sync_fix_track(
                fix_job_id=str(fix.get("job_id") or ""),
                track_index=int(fix["track_index"]),
                status="skipped",
                detail="Skipped by user",
                adjust_failed=-1,
            )
            job.remove_open_fix(fix_id)
            if not job.running:
                job.status = f"Skipped · {skip_title}"
            job.bump()
        else:
            if action == "paste_url" and not (body.youtube_url or "").strip():
                raise HTTPException(status_code=400, detail="youtube_url is required")

            options = dict(fix.get("options") or {})
            browser = (
                body.browser
                if body.browser and body.browser != "none"
                else options.get("browser")
            )
            dest = (
                Path(str(fix["output_dir"]))
                if fix.get("output_dir")
                else last_output_dir
            )
            fix["status"] = "fixing"
            fix["detail"] = {
                "retry": "Retrying search",
                "loose": "Loose search (any duration)",
                "paste_url": "Downloading pasted URL",
            }.get(action, "Fixing")
            job.remediating = True
            job.fix_cancel_flag = False
            if not job.running:
                job.status = f"Fixing · {fix.get('title')}"
                job.file_percent = 0
            job.sync_fix_track(
                fix_job_id=str(fix.get("job_id") or ""),
                track_index=int(fix["track_index"]),
                status="active",
                detail=str(fix["detail"]),
                adjust_failed=-1,
            )
            launch = {
                "fix_id": str(fix["id"]),
                "track_index": int(fix["track_index"]),
                "browser": browser,
                "dest": dest,
            }
            job.bump()

    if action == "skip":
        job.append_log(f"Skipped fix · {skip_title}", parse=False)
        return {"ok": True, "status": job.snapshot()}

    assert launch is not None
    thread = threading.Thread(
        target=_run_remediate,
        kwargs={
            "fix_id": launch["fix_id"],
            "track_index": launch["track_index"],
            "action": action,
            "youtube_url": (body.youtube_url or "").strip() or None,
            "browser": launch["browser"],
            "dest": launch["dest"],
        },
        daemon=True,
    )
    thread.start()
    return {"ok": True, "status": job.snapshot()}


@app.post("/api/cancel")
async def cancel_download() -> dict[str, Any]:
    with job.lock:
        if not job.running and not job.remediating:
            return {"ok": True, "status": job.snapshot()}
        if job.running:
            # Cancel only the active album download — never the background fix.
            job.cancel_flag = True
            job.status = "Cancelling…"
            msg = "Cancel requested for current download…"
        else:
            job.fix_cancel_flag = True
            job.status = "Cancelling fix…"
            msg = "Cancel requested for fix…"
    job.append_log(msg, parse=False)
    return {"ok": True, "status": job.snapshot()}


@app.post("/api/tracks/skip")
async def skip_queued_track(body: TrackSkipBody) -> dict[str, Any]:
    """Skip a pending song in the current download (before it starts)."""
    with job.lock:
        if not job.running:
            raise HTTPException(status_code=400, detail="No download in progress")
        row = None
        for track in job.tracks:
            if track.get("index") == body.track_index:
                row = track
                break
        if row is None:
            raise HTTPException(status_code=404, detail="Track not found in this job")
        if row.get("status") != "pending":
            raise HTTPException(
                status_code=400,
                detail="Only queued songs can be skipped (not the one currently downloading)",
            )
        job.skip_indices.add(body.track_index)
        if row.get("status") != "skipped":
            job.skipped += 1
        row["status"] = "skipped"
        row["detail"] = "Skipped by user"
        title = row.get("title") or f"Track {body.track_index}"
        job.bump()
    job.append_log(f"Queued skip · #{body.track_index:03d} {title}", parse=False)
    return {"ok": True, "status": job.snapshot()}


def _run_download(
    *,
    url: str,
    dest: Path,
    options: dict[str, Any],
) -> None:
    def log(message: str) -> None:
        job.append_log(message)

    def progress(completed: int, total: int) -> None:
        job.set_progress(completed, total)

    def file_progress(fraction: float) -> None:
        job.set_file_progress(fraction)

    def should_cancel() -> bool:
        return job.cancel_flag

    def should_skip_track(track_index: int) -> bool:
        with job.lock:
            return track_index in job.skip_indices

    def on_tracks(tracks: list[Track]) -> None:
        job.set_track_models(tracks)

    def on_track_model(track: Track) -> None:
        with job.lock:
            job.track_models[track.index] = track

    callbacks = DownloadCallbacks(
        log=log,
        progress=progress,
        file_progress=file_progress,
        should_cancel=should_cancel,
        should_skip_track=should_skip_track,
        on_tracks=on_tracks,
        on_track_model=on_track_model,
    )
    cover_bytes, cover_mime = _load_cover_bytes(
        options.get("cover_path"),
        options.get("cover_url"),
    )
    album_name = str(options.get("album") or "").strip()
    artist_name = str(options.get("artist") or "").strip()
    compilation = _is_compilation(options)
    # Album artist defaults to the Link artist when blank.
    album_artist = str(options.get("album_artist") or "").strip() or artist_name
    code = 1
    try:
        code = download_url(
            url=url,
            output_dir=dest,
            keep_original=False,
            browser=options.get("browser"),
            skip_existing=not bool(options.get("force")),
            limit=options.get("limit"),
            tracks_spec=options.get("tracks"),
            embed_metadata=bool(options.get("embed_metadata", True)),
            playlist_track_numbers=bool(options.get("playlist_track_numbers", True)),
            debug=bool(options.get("debug", False)),
            callbacks=callbacks,
            album_override=album_name or None,
            artists_override=None if compilation else (artist_name or None),
            album_artist=album_artist or None,
            compilation=compilation,
            strip_terms=str(options.get("strip_terms") or ""),
            verify_artists=bool(options.get("verify_artists")),
            track_plan=options.get("track_plan") or None,
            cover_bytes=cover_bytes,
            cover_mime=cover_mime,
        )
    except Exception as exc:  # noqa: BLE001
        job.append_log(f"Error: {exc}")
        code = 1
    finally:
        n_open = 0
        with job.lock:
            job.running = False
            if job._worker_thread is threading.current_thread():
                job._worker_thread = None
            # Close any song left stuck on "Downloading" after a missed log line
            # or an older remediation race that stole _active_index.
            try:
                job.finalize_open_tracks(cancelled=bool(job.cancel_flag))
            except Exception as exc:  # noqa: BLE001
                job.logs.append(f"Warning: finalize_open_tracks failed: {exc}")
            for track in job.tracks:
                if track.get("status") == "failed":
                    job.upsert_open_fix(
                        int(track["index"]), str(track.get("detail") or "Failed")
                    )
            n_open = sum(1 for f in job.open_fixes if f.get("status") == "failed")
            if job.cancel_flag:
                job.status = "Cancelled"
                hist_status = "cancelled"
            elif code == 0:
                job.progress = 1.0
                job.percent = 100
                job.status = "Done"
                hist_status = "done"
            else:
                job.status = (
                    f"Finished with {job.failed} error(s) — queue continues"
                    if job.failed
                    else "Finished with errors — queue continues"
                )
                hist_status = "failed"
            summary = job._current_summary() or {}
            summary["status"] = hist_status
            summary["percent"] = job.percent
            summary["status_text"] = job.status
            job.history.appendleft(summary)
            # Filed into history now — the queue's "current job" card should
            # clear rather than keep showing this finished job.
            job.job_finished = True
            job.bump()
        if n_open:
            job.append_log(f"{n_open} track(s) left in Failed panel to fix anytime.")
        # Always advance the queue; failures stay in the Failed panel.
        try:
            _ensure_queue_worker()
        except Exception as exc:  # noqa: BLE001
            with job.lock:
                job.logs.append(f"Error starting next queue job: {exc}")
                job.bump()


@app.post("/api/queue/continue")
async def queue_continue() -> dict[str, Any]:
    """Nudge the queue worker (idempotent; queue no longer pauses on failures)."""
    with job.lock:
        if job.running:
            return {"ok": True, "status": job.snapshot()}
        if not job.pending:
            return {"ok": True, "status": job.snapshot()}
    _ensure_queue_worker()
    return {"ok": True, "status": job.snapshot()}


def _run_remediate(
    *,
    fix_id: str,
    track_index: int,
    action: str,
    youtube_url: str | None,
    browser: str | None,
    dest: Path,
) -> None:
    def log(message: str) -> None:
        # Critical: never parse fix output into the active download track state.
        job.append_fix_log(message)

    def file_progress(fraction: float) -> None:
        # Never touch the download progress meter while an album is downloading.
        with job.lock:
            if job.running:
                return
        job.set_file_progress(fraction)

    def should_cancel() -> bool:
        return bool(job.fix_cancel_flag)

    with job.lock:
        fix = job._find_open_fix(fix_id=fix_id)
        model = fix.get("model") if fix else None
        options = dict((fix or {}).get("options") or {})
        artist = str((fix or {}).get("artist") or options.get("artist") or "")
        album = str((fix or {}).get("album") or options.get("album") or "")
        fix_job_id = str((fix or {}).get("job_id") or "")

    if model is None or fix is None:
        with job.lock:
            job.remediating = False
            if fix is not None:
                fix["status"] = "failed"
                fix["detail"] = "Track metadata unavailable"
            if not job.running:
                job.status = "Remediation failed"
            job.bump()
        return

    job.append_fix_log(f"Fixing #{track_index} · {model.title} → {artist}/{album}")

    cover_bytes, cover_mime = _load_cover_bytes(
        options.get("cover_path"),
        options.get("cover_url"),
    )
    callbacks = DownloadCallbacks(
        log=log,
        file_progress=file_progress,
        should_cancel=should_cancel,
    )
    result_status = "failed"
    result_detail = "Failed"
    try:
        result = remediate_track(
            model,
            dest,
            browser=browser,
            embed_metadata=bool(options.get("embed_metadata", True)),
            playlist_track_numbers=bool(options.get("playlist_track_numbers", True)),
            debug=bool(options.get("debug", False)),
            youtube_url=youtube_url if action == "paste_url" else None,
            loose_match=action == "loose",
            callbacks=callbacks,
            album_override=album or None,
            artists_override=None if _is_compilation(options) else (artist or None),
            album_artist=(
                str(options.get("album_artist") or "").strip() or artist or None
            ),
            compilation=_is_compilation(options),
            cover_bytes=cover_bytes,
            cover_mime=cover_mime,
        )
        result_status = result.status
        result_detail = result.detail or result.status
    except Exception as exc:  # noqa: BLE001
        job.append_fix_log(f"Error: {exc}")
        result_status = "failed"
        result_detail = str(exc)
    finally:
        with job.lock:
            fix = job._find_open_fix(fix_id=fix_id)
            if result_status == "done":
                if fix is not None:
                    job.remove_open_fix(fix_id)
                job.sync_fix_track(
                    fix_job_id=fix_job_id,
                    track_index=track_index,
                    status="done",
                    detail=result_detail,
                )
                if not job.running:
                    job.status = f"Fixed · {model.title}"
            elif result_status == "cancelled":
                if fix is not None:
                    fix["status"] = "failed"
                    fix["detail"] = "Cancelled"
                job.sync_fix_track(
                    fix_job_id=fix_job_id,
                    track_index=track_index,
                    status="failed",
                    detail="Cancelled",
                    adjust_failed=1,
                )
                if not job.running:
                    job.status = "Fix cancelled"
            else:
                if fix is not None:
                    fix["status"] = "failed"
                    fix["detail"] = result_detail
                job.sync_fix_track(
                    fix_job_id=fix_job_id,
                    track_index=track_index,
                    status="failed",
                    detail=result_detail,
                    adjust_failed=1,
                )
                if not job.running:
                    job.status = f"Still failed · {model.title}"
            job.remediating = False
            job.fix_cancel_flag = False
            if not job.running:
                job.file_percent = 0
            job.bump()
        if result_status == "done":
            job.append_fix_log(
                f"saved into {artist}/{album}"
                + (
                    f" (playlist # {track_index})"
                    if options.get("playlist_track_numbers")
                    else ""
                )
            )


def main() -> None:
    import uvicorn

    host = resolve_bind_host()
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    os.environ["_ACTIVE_BIND"] = f"{host}:{port}"
    default_output_dir.mkdir(parents=True, exist_ok=True)

    print("Music Downloader web UI")
    print(f"  bind:        http://{host}:{port}")
    print(f"  output dir:  {default_output_dir} (overridable per download in the UI)")
    print(f"  localhost:   {'allowed' if allow_localhost else 'blocked'}")
    print(
        "  client ACL:  "
        + ("Tailscale CGNAT only" if require_tailscale_client else "disabled")
    )
    if running_in_docker():
        print("  docker:      yes — publish the port only on your Tailscale IP if possible")
    elif host.startswith("100."):
        print("  access:      Tailscale devices only (bound to tailnet IP)")
    else:
        print("  access:      local only until Tailscale IP is available")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
