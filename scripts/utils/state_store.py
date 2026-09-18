"""
Tiny, dependency-free key -> value state store backed by a JSON file.

Used to make garage_uploader.py, garage_sync.py, and ec2_poller.py
idempotent across restarts: "have I already handled this file/key?"
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def _flush(self) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any = True) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def remove(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._flush()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
