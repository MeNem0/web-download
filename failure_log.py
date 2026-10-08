"""Persistent log of failed downloads and every attempt to recover them.

One JSON object per line in ``failures.jsonl`` (machine readable, survives
restarts), mirrored in a small in-memory window so the UI can poll it cheaply.
Events, in the order a song normally goes through them:

    failed        the song failed its first pass in an album download
    retry_start   an automatic or manual retry began
    recovered     a retry produced the MP3 - the song is no longer a problem
    retry_failed  a retry also failed (it may be retried again)
    gave_up       automatic retries are exhausted; the song needs the user
    skipped       the user chose to skip the song
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

EVENTS = ("failed", "retry_start", "recovered", "retry_failed", "gave_up", "skipped")
# Keep the file bounded: past this size it is rotated to failures.jsonl.1.
MAX_BYTES = 2_000_000
WINDOW = 2_000


def _default_dir() -> Path:
    configured = os.environ.get("LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "logs"


class FailureLog:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or _default_dir()).resolve()
        self.path = self.directory / "failures.jsonl"
        self._lock = threading.Lock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=WINDOW)
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-WINDOW:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._entries.append(entry)
                self._next_id = max(self._next_id, int(entry.get("id") or 0) + 1)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append an event. Never raises: logging must not break a download."""
        entry: dict[str, Any] = {
            "id": 0,
            "ts": round(time.time(), 3),
            "event": event if event in EVENTS else "failed",
        }
        entry.update({k: v for k, v in fields.items() if v not in (None, "")})
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # keep the in-memory copy; the disk may be read-only/offline
        return entry

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
        except OSError:
            return
        backup = self.path.with_suffix(".jsonl.1")
        try:
            backup.unlink(missing_ok=True)
            self.path.replace(backup)
        except OSError:
            pass

    def entries(
        self,
        *,
        limit: int = 200,
        event: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        """Newest first, optionally filtered by event and a text query."""
        wanted = {e for e in event.split(",") if e}
        needle = query.strip().lower()
        with self._lock:
            snapshot = list(self._entries)
        out: list[dict[str, Any]] = []
        for entry in reversed(snapshot):
            if wanted and entry.get("event") not in wanted:
                continue
            if needle:
                haystack = " ".join(
                    str(entry.get(k) or "") for k in ("title", "artist", "album", "error", "detail")
                ).lower()
                if needle not in haystack:
                    continue
            out.append(entry)
            if len(out) >= max(1, limit):
                break
        return out

    def counts(self) -> dict[str, int]:
        with self._lock:
            snapshot = list(self._entries)
        counts = {event: 0 for event in EVENTS}
        for entry in snapshot:
            name = entry.get("event")
            if name in counts:
                counts[name] += 1
        counts["total"] = len(snapshot)
        return counts

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            try:
                self.path.unlink(missing_ok=True)
                self.path.with_suffix(".jsonl.1").unlink(missing_ok=True)
            except OSError:
                pass

    def export_text(self) -> str:
        """Human-readable copy, oldest first, for reading in any editor."""
        with self._lock:
            snapshot = list(self._entries)
        lines = []
        for e in snapshot:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(e.get("ts") or 0)))
            where = " / ".join(p for p in (e.get("artist"), e.get("album")) if p)
            number = f"#{int(e['index']):02d} " if e.get("index") else ""
            rnd = f" [round {e['round']}/{e['rounds']}]" if e.get("round") and e.get("rounds") else ""
            how = f" ({e['action']})" if e.get("action") and e.get("action") != "auto" else ""
            head = f"{when}  {str(e.get('event', '')).upper():<12} {where} {number}{e.get('title', '')}{rnd}{how}"
            lines.append(head.rstrip())
            if e.get("error") or e.get("detail"):
                lines.append(f"    {e.get('error') or e.get('detail')}")
            if e.get("methods"):
                lines.append(f"    methods tried: {', '.join(e['methods'])}")
        return "\n".join(lines) + ("\n" if lines else "")
