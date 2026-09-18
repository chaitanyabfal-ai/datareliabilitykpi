"""
Maintenance calendar store -- a tiny dependency-free registry of planned
operational maintenance windows (maintenance, pigging, valve operations,
sensor recalibration, emergency shutdowns).

Used by the operational detector to cross-reference downtime against planned
windows and by the uptime engine to exclude planned downtime from the
uptime denominator. Windows are persisted as JSON in MAINTENANCE_LOG_PATH
so operators (or a future Google Calendar/scheduling integration) can write
them and the detectors read them.

Window shape:
    {"id": str, "start": iso, "end": iso, "cause": str,
     "sensor_ids": [str,...] or [] for all, "note": str}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import Lock

from config.aws_config import MAINTENANCE_LOG_PATH
from scripts.utils.uptime_engine import parse_timestamp


class MaintenanceCalendar:
    def __init__(self, path: str | Path = MAINTENANCE_LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._windows: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    self._windows = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._windows = []
        else:
            self._windows = []

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._windows, f, indent=2)
        tmp.replace(self.path)

    def add_window(
        self,
        start: str | datetime,
        end: str | datetime,
        cause: str,
        sensor_ids: list[str] | None = None,
        note: str = "",
        window_id: str | None = None,
    ) -> dict:
        s = parse_timestamp(start)
        e = parse_timestamp(end)
        if s is None or e is None:
            raise ValueError("maintenance window start/end could not be parsed")
        entry = {
            "id": window_id or f"{cause}-{s.isoformat()}-{e.isoformat()}",
            "start": s.isoformat(),
            "end": e.isoformat(),
            "cause": cause,
            "sensor_ids": sensor_ids or [],
            "note": note,
        }
        with self._lock:
            self._windows.append(entry)
            self._flush()
        return entry

    def windows(self) -> list[dict]:
        with self._lock:
            return list(self._windows)

    def overlapping(
        self,
        start: datetime,
        end: datetime,
        sensor_id: str | None = None,
    ) -> list[dict]:
        """Return maintenance windows that overlap [start, end], optionally
        filtered to those that apply to `sensor_id` (empty sensor_ids = all)."""
        hits: list[dict] = []
        for w in self.windows():
            ws = parse_timestamp(w["start"])
            we = parse_timestamp(w["end"])
            if ws is None or we is None:
                continue
            if we < start or ws > end:
                continue
            ids = w.get("sensor_ids", [])
            if sensor_id is not None and ids and sensor_id not in ids:
                continue
            hits.append(w)
        return hits


_DEFAULT: MaintenanceCalendar | None = None


def get_default_calendar() -> MaintenanceCalendar:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MaintenanceCalendar()
    return _DEFAULT
