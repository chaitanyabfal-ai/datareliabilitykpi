"""
Downtime event log -- an append-only JSONL store of classified downtime
events used by the dashboard (downtime root-cause breakdown, timeline) and
by the uptime engine (planned/uncontrollable exclusions).

Event shape:
    {"sensor_id": str, "start": iso, "end": iso, "duration_seconds": float,
     "cause": str, "category": str, "confidence": float, "evidence": dict,
     "classified_at": iso}

Stored as newline-delimited JSON (JSONL) so events can be appended cheaply
without rewriting the whole file, and loaded back as a list for reporting.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config.aws_config import CAUSE_LOG_PATH
from scripts.utils.uptime_engine import parse_timestamp


class DowntimeEventLog:
    def __init__(self, path: str | Path = CAUSE_LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, event: dict) -> None:
        line = json.dumps(event)
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def load(self) -> list[dict]:
        events: list[dict] = []
        if not self.path.exists():
            return events
        with self._lock:
            with open(self.path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return events

    def load_for_sensor(self, sensor_id: str) -> list[dict]:
        return [e for e in self.load() if e.get("sensor_id") == sensor_id]

    def clear(self) -> None:
        with self._lock:
            self.path.write_text("")


def log_downtime(
    sensor_id: str,
    start,
    end,
    cause: str,
    category: str,
    confidence: float = 1.0,
    evidence: dict | None = None,
    log: DowntimeEventLog | None = None,
) -> dict:
    """Append one classified downtime event to the log and return it."""
    s = parse_timestamp(start)
    e = parse_timestamp(end)
    if s is None or e is None:
        raise ValueError("downtime start/end could not be parsed")
    if e < s:
        s, e = e, s
    event = {
        "sensor_id": sensor_id,
        "start": s.isoformat(),
        "end": e.isoformat(),
        "duration_seconds": (e - s).total_seconds(),
        "cause": cause,
        "category": category,
        "confidence": confidence,
        "evidence": evidence or {},
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }
    (log or DowntimeEventLog()).append(event)
    return event
