"""Remember each MP3's tags so browsing a big library doesn't re-read every file.

Reading ID3 tags opens the file, which over a network share costs a round trip
per song, so listing a 100-song folder used to take minutes. A file's tags only
change when the file does, so they are cached under (path, size, modified time)
and re-read automatically when either changes - including edits made in this app.
Reads that do have to happen run in parallel, so their latency overlaps.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

WORKERS = 12
SAVE_INTERVAL = 20.0
MAX_ENTRIES = 60_000


class TagCache:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "tagcache.json"
        self._data: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self._saver: threading.Timer | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 3}

    def lookup(self, key: str, mtime_ns: int, size: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._data.get(key)
        if row and row[0] == mtime_ns and row[1] == size:
            return dict(row[2])
        return None

    def store(self, key: str, mtime_ns: int, size: int, meta: dict[str, Any]) -> None:
        with self._lock:
            if len(self._data) >= MAX_ENTRIES and key not in self._data:
                self._data.pop(next(iter(self._data)), None)
            self._data[key] = [mtime_ns, size, dict(meta)]
            self._dirty = True
        self._save_soon()

    def forget_under(self, prefix: str) -> None:
        """Drop entries for a path that was renamed or deleted."""
        norm = prefix.replace("\\", "/")
        with self._lock:
            for key in [k for k in self._data if k.replace("\\", "/").startswith(norm)]:
                del self._data[key]
            self._dirty = True
        self._save_soon()

    def _save_soon(self) -> None:
        with self._lock:
            if self._saver is not None:
                return
            delay = max(1.0, SAVE_INTERVAL - (time.monotonic() - self._last_save))
            self._saver = threading.Timer(delay, self._save)
            self._saver.daemon = True
            self._saver.start()

    def _save(self) -> None:
        with self._lock:
            self._saver = None
            if not self._dirty:
                return
            snapshot = dict(self._data)
            self._dirty = False
            self._last_save = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # the cache is an optimisation; losing a save is harmless

    def read_many(
        self,
        entries: Iterable[os.DirEntry[str]],
        reader: Callable[[Path], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Tags for each directory entry, in order: cached where possible,
        the rest read in parallel."""
        items = list(entries)

        def one(entry: os.DirEntry[str]) -> dict[str, Any]:
            try:
                stat = entry.stat()
            except OSError:
                return reader(Path(entry.path))
            hit = self.lookup(entry.path, stat.st_mtime_ns, stat.st_size)
            if hit is not None:
                return hit
            meta = reader(Path(entry.path))
            self.store(entry.path, stat.st_mtime_ns, stat.st_size, meta)
            return meta

        if len(items) <= 1:
            return [one(entry) for entry in items]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(one, items))

    def read_one(self, path: Path, reader: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
        """Same, for a single path with no directory entry in hand."""
        try:
            stat = path.stat()
        except OSError:
            return reader(path)
        key = str(path)
        hit = self.lookup(key, stat.st_mtime_ns, stat.st_size)
        if hit is not None:
            return hit
        meta = reader(path)
        self.store(key, stat.st_mtime_ns, stat.st_size, meta)
        return meta
