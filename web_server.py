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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

TRACK_LINE_RE = re.compile(
    r"^\[(\d{3})\] (duplicate, skipping|searching|downloading):\s*(.+)$"
)
SAVED_LINE_RE = re.compile(
    r"^ {2}saved(?: with metadata| \(metadata failed:[^)]*\))?:\s*(.+)$"
)
FAILED_LINE_RE = re.compile(
    r"^ {2}failed(?: after \d+ attempt\(s\))?:\s*(.+)$"
)
FAILED_NO_URL = "  failed: no download URL"
NO_RESULTS_PREFIXES = (
    "  no YouTube search results",
    "  no result within ",
)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from metadata_tags import read_cover_bytes, read_track_metadata, update_track_metadata
from music_downloader import DownloadCallbacks, Track, download_url, remediate_track

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


@dataclass
class JobState:
    running: bool = False
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
    tracks: list[dict[str, Any]] = field(default_factory=list)
    track_models: dict[int, Track] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    cancel_flag: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    version: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)
    _active_index: int | None = None

    def reset_job(self, *, url: str, output_dir: str, options: dict[str, Any] | None = None) -> None:
        self.running = True
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
        self.tracks.clear()
        self.track_models.clear()
        self.options = dict(options or {})
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
                row["remediable"] = True
            self.bump()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            can_fix = not self.running and bool(self.track_models)
            tracks_out = []
            for track in self.tracks:
                item = dict(track)
                idx = item.get("index")
                item["remediable"] = bool(
                    can_fix
                    and idx in self.track_models
                    and item.get("status") in {"failed", "skipped"}
                )
                tracks_out.append(item)
            return {
                "running": self.running,
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
                "tracks": tracks_out,
                "log": list(self.logs),
                "version": self.version,
                "playlist_track_numbers": bool(
                    self.options.get("playlist_track_numbers")
                ),
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

    def _finish_active(self, status: str, detail: str = "") -> bool:
        if self._active_index is None:
            return False
        track = self._track_by_index(self._active_index)
        if track["status"] in {"done", "skipped", "failed"}:
            return False
        track["status"] = status
        if detail:
            track["detail"] = detail
        return True

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
            track = self._track_by_index(index)
            track["title"] = title
            self._active_index = index
            if action == "duplicate, skipping":
                track["status"] = "skipped"
                track["detail"] = "Already downloaded"
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
            self._finish_active("done", saved.group(1).strip())
            self._active_index = None
            return

        if line == FAILED_NO_URL or any(line.startswith(p) for p in NO_RESULTS_PREFIXES):
            detail = line.strip()
            if self._finish_active("failed", detail):
                self.failed += 1
            self._active_index = None
            return

        failed = FAILED_LINE_RE.match(line)
        if failed:
            if self._finish_active("failed", "Failed"):
                self.failed += 1
            self._active_index = None

    def append_log(self, message: str) -> None:
        with self.lock:
            for line in message.splitlines() or [message]:
                self.logs.append(line)
                self._ingest_line(line)
            self.bump()

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
    output_dir: str | None = None
    force: bool = False
    embed_metadata: bool = True
    playlist_track_numbers: bool = False
    debug: bool = False
    limit: int | None = Field(default=None, ge=1, le=10_000)
    browser: str | None = None


class RemediateRequest(BaseModel):
    track_index: int = Field(ge=1)
    action: str = Field(min_length=1)  # retry | loose | paste_url | skip
    youtube_url: str | None = None
    browser: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    snap = job.snapshot()
    if not snap.get("output_dir"):
        snap["output_dir"] = str(last_output_dir)
    snap["default_output_dir"] = str(default_output_dir)
    root = browse_root()
    snap["browse_root"] = str(root) if root else None
    snap["docker"] = running_in_docker()
    snap["bind_hint"] = os.environ.get("_ACTIVE_BIND", "")
    return snap


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
    return list_library_entries(path)


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
    track_number: str = ""
    cover_url: str | None = None
    remove_cover: bool = False


class LibraryMetaBulkUpdate(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    title: str | None = None
    artists: str | None = None
    album: str | None = None
    track_number: str | None = None
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
            track_number=body.track_number,
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
                track_number=track_number,
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


@app.post("/api/download")
async def start_download(body: DownloadRequest) -> dict[str, Any]:
    global last_output_dir
    dest = resolve_output_dir(body.output_dir)
    last_output_dir = dest
    browser = body.browser if body.browser and body.browser != "none" else None
    options = {
        "force": body.force,
        "embed_metadata": body.embed_metadata,
        "playlist_track_numbers": body.playlist_track_numbers,
        "debug": body.debug,
        "limit": body.limit,
        "browser": browser,
    }

    with job.lock:
        if job.running:
            raise HTTPException(status_code=409, detail="A download is already running")
        job.reset_job(url=body.url.strip(), output_dir=str(dest), options=options)

    thread = threading.Thread(
        target=_run_download,
        kwargs={
            "url": body.url.strip(),
            "dest": dest,
            "force": body.force,
            "embed_metadata": body.embed_metadata,
            "playlist_track_numbers": body.playlist_track_numbers,
            "debug": body.debug,
            "limit": body.limit,
            "browser": browser,
        },
        daemon=True,
    )
    thread.start()
    return {"ok": True, "status": job.snapshot()}


@app.post("/api/remediate")
async def start_remediate(body: RemediateRequest) -> dict[str, Any]:
    action = body.action.strip().lower()
    if action not in {"retry", "loose", "paste_url", "skip"}:
        raise HTTPException(
            status_code=400,
            detail="action must be retry, loose, paste_url, or skip",
        )

    with job.lock:
        if job.running:
            raise HTTPException(
                status_code=409,
                detail="Wait for the current download to finish before fixing tracks",
            )
        model = job.track_models.get(body.track_index)
        if model is None:
            raise HTTPException(
                status_code=404,
                detail="Track metadata unavailable. Re-run the playlist download first.",
            )
        row = job._track_by_index(body.track_index)
        if row.get("status") not in {"failed", "skipped"}:
            raise HTTPException(
                status_code=400,
                detail="Only failed or skipped tracks can be remediated",
            )

        if action == "skip":
            row["status"] = "skipped"
            row["detail"] = "Skipped by user"
            job.status = f"Skipped · {model.title}"
            job.bump()
            return {"ok": True, "status": job.snapshot()}

        if action == "paste_url" and not (body.youtube_url or "").strip():
            raise HTTPException(status_code=400, detail="youtube_url is required")

        options = dict(job.options)
        browser = body.browser if body.browser and body.browser != "none" else options.get("browser")
        dest = Path(job.output_dir) if job.output_dir else last_output_dir
        job.running = True
        job.cancel_flag = False
        job.status = f"Fixing · {model.title}"
        job.file_percent = 0
        if row.get("status") == "failed" and job.failed > 0:
            job.failed -= 1
        row["status"] = "active"
        row["detail"] = {
            "retry": "Retrying search",
            "loose": "Loose search (any duration)",
            "paste_url": "Downloading pasted URL",
        }.get(action, "Fixing")
        job._active_index = body.track_index
        job.bump()

    thread = threading.Thread(
        target=_run_remediate,
        kwargs={
            "track_index": body.track_index,
            "action": action,
            "youtube_url": (body.youtube_url or "").strip() or None,
            "browser": browser,
            "dest": dest,
        },
        daemon=True,
    )
    thread.start()
    return {"ok": True, "status": job.snapshot()}


@app.post("/api/cancel")
async def cancel_download() -> dict[str, Any]:
    with job.lock:
        if not job.running:
            return {"ok": True, "status": job.snapshot()}
        job.cancel_flag = True
        job.status = "Cancelling…"
    job.append_log("Cancel requested…")
    return {"ok": True, "status": job.snapshot()}


def _run_download(
    *,
    url: str,
    dest: Path,
    force: bool,
    embed_metadata: bool,
    playlist_track_numbers: bool,
    debug: bool,
    limit: int | None,
    browser: str | None,
) -> None:
    def log(message: str) -> None:
        job.append_log(message)

    def progress(completed: int, total: int) -> None:
        job.set_progress(completed, total)

    def file_progress(fraction: float) -> None:
        job.set_file_progress(fraction)

    def should_cancel() -> bool:
        return job.cancel_flag

    def on_tracks(tracks: list[Track]) -> None:
        job.set_track_models(tracks)

    callbacks = DownloadCallbacks(
        log=log,
        progress=progress,
        file_progress=file_progress,
        should_cancel=should_cancel,
        on_tracks=on_tracks,
    )
    code = 1
    try:
        code = download_url(
            url=url,
            output_dir=dest,
            keep_original=False,
            browser=browser,
            skip_existing=not force,
            limit=limit,
            embed_metadata=embed_metadata,
            playlist_track_numbers=playlist_track_numbers,
            debug=debug,
            callbacks=callbacks,
        )
    except Exception as exc:  # noqa: BLE001
        job.append_log(f"Error: {exc}")
        code = 1
    finally:
        with job.lock:
            job.running = False
            job._active_index = None
            if job.cancel_flag:
                job.status = "Cancelled"
            elif code == 0:
                job.progress = 1.0
                job.percent = 100
                if job.status.startswith("Track") or job.status.startswith("Starting"):
                    job.status = "Done"
            else:
                job.status = "Finished with errors — fix failed tracks below"
            job.bump()


def _run_remediate(
    *,
    track_index: int,
    action: str,
    youtube_url: str | None,
    browser: str | None,
    dest: Path,
) -> None:
    def log(message: str) -> None:
        job.append_log(message)

    def file_progress(fraction: float) -> None:
        job.set_file_progress(fraction)

    def should_cancel() -> bool:
        return job.cancel_flag

    with job.lock:
        model = job.track_models.get(track_index)
        options = dict(job.options)
    if model is None:
        with job.lock:
            job.running = False
            job.status = "Remediation failed"
            job.bump()
        return

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
            playlist_track_numbers=bool(options.get("playlist_track_numbers", False)),
            debug=bool(options.get("debug", False)),
            youtube_url=youtube_url if action == "paste_url" else None,
            loose_match=action == "loose",
            callbacks=callbacks,
        )
        result_status = result.status
        result_detail = result.detail or result.status
    except Exception as exc:  # noqa: BLE001
        job.append_log(f"Error: {exc}")
        result_status = "failed"
        result_detail = str(exc)
    finally:
        with job.lock:
            row = job._track_by_index(track_index)
            if result_status == "done":
                row["status"] = "done"
                row["detail"] = result_detail
                job.status = f"Fixed · {model.title}"
            elif result_status == "cancelled":
                if row.get("status") != "failed":
                    row["status"] = "failed"
                    row["detail"] = "Cancelled"
                    job.failed += 1
                job.status = "Cancelled"
            else:
                if row.get("status") != "failed":
                    row["status"] = "failed"
                    row["detail"] = result_detail
                    job.failed += 1
                else:
                    row["detail"] = result_detail
                job.status = f"Still failed · {model.title}"
            job.running = False
            job._active_index = None
            job.file_percent = 0
            job.bump()
        if result_status == "done" and options.get("playlist_track_numbers"):
            job.append_log(f"  kept playlist track # {track_index} on {model.title}")


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
