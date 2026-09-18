"""
Reliability reporter -- orchestrates uptime computation and downtime
detection/classification for a sensor over a time window.

Given a sensor's records (and the configured cadence/heartbeat), it:
  1. Computes per-sensor uptime KPIs (timestamp consonance, differ rate,
     heartbeat, data-quality, correlation) via uptime_engine.compute_uptime.
  2. Detects downtime gaps (expected points not received) from the timeline.
  3. Classifies each gap with the downtime classifier + detectors.
  4. Logs classified downtime events and writes an uptime report.

This is the bridge the aggregator calls for hourly/daily reliability rollups
and the dashboard reads. All logic is rule-based -- no machine learning.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.aws_config import KPI_UPTIME_DIR, RELIABILITY
from scripts.utils.downtime_classifier import (
    DowntimeClassifier,
    classify_downtime_event,
    cause_bucket,
)
from scripts.utils.downtime_event_log import DowntimeEventLog, log_downtime
from scripts.utils.uptime_engine import (
    compute_uptime,
    parse_timestamp,
    uptime_with_exclusions,
)


def detect_downtime_gaps(
    sensor_id: str,
    records: list[dict],
    window_start: datetime,
    window_end: datetime,
    cadence_seconds: int | None = None,
) -> list[tuple[datetime, datetime]]:
    """Find gaps where no data was received for longer than the heartbeat interval.

    A gap starts at the expected next reading after the last seen timestamp and
    ends at the next seen timestamp (or the window end if the sensor never
    recovered within the window).
    """
    heartbeat = RELIABILITY.heartbeat_interval_seconds
    timestamps = sorted(
        ts for ts in (parse_timestamp(r.get("timestamp")) for r in records)
        if ts is not None and window_start <= ts <= window_end
    )
    gaps: list[tuple[datetime, datetime]] = []
    if not timestamps:
        # Whole window is a gap only if it is longer than the heartbeat.
        if (window_end - window_start).total_seconds() > heartbeat:
            gaps.append((window_start, window_end))
        return gaps

    cursor = window_start
    for ts in timestamps:
        if ts > cursor + timedelta(seconds=heartbeat):
            gaps.append((cursor, ts))
        cursor = ts
    if window_end > cursor + timedelta(seconds=heartbeat):
        gaps.append((cursor, window_end))
    return gaps


def report_reliability(
    records: list[dict],
    window_start: datetime,
    window_end: datetime,
    context: dict | None = None,
    classifier: DowntimeClassifier | None = None,
    event_log: DowntimeEventLog | None = None,
    write_reports: bool = True,
) -> dict:
    """Compute uptime + classify downtime for one window and (optionally)
    persist reports to KPI_UPTIME_DIR / KPI_DOWNTIME_DIR and the event log."""
    context = context or {}
    now = datetime.now(timezone.utc)
    uptime_result = compute_uptime(records, window_start, window_end, now=now)

    # Per-sensor downtime detection + classification.
    by_sensor: dict[str, list[dict]] = {}
    for rec in records:
        sid = rec.get("sensor_id")
        if sid is None:
            continue
        by_sensor.setdefault(sid, []).append(rec)

    downtime_events: list[dict] = []
    for sensor_id, recs in by_sensor.items():
        gaps = detect_downtime_gaps(sensor_id, recs, window_start, window_end)
        for gap_start, gap_end in gaps:
            detection = classify_downtime_event(
                sensor_id, gap_start, gap_end, context=context, classifier=classifier
            )
            event = log_downtime(
                sensor_id=sensor_id,
                start=gap_start,
                end=gap_end,
                cause=detection.cause,
                category=detection.category,
                confidence=detection.confidence,
                evidence=detection.evidence,
                log=event_log,
            )
            downtime_events.append(event)

    # Also handle sensors we know about but received NO data for in the window.
    known_sensors = context.get("known_sensors") or []
    for sensor_id in known_sensors:
        if sensor_id in by_sensor:
            continue
        if (window_end - window_start).total_seconds() <= RELIABILITY.heartbeat_interval_seconds:
            continue
        detection = classify_downtime_event(
            sensor_id, window_start, window_end,
            context={**context, "no_data_at_all": True}, classifier=classifier,
        )
        event = log_downtime(
            sensor_id=sensor_id, start=window_start, end=window_end,
            cause=detection.cause, category=detection.category,
            confidence=detection.confidence, evidence=detection.evidence, log=event_log,
        )
        downtime_events.append(event)

    # Uptime with planned/uncontrollable exclusions per sensor.
    exclusions: dict[str, dict] = {}
    all_events = (event_log.load() if event_log else []) + downtime_events
    for sensor_id in uptime_result.sensors:
        sensor_events = [e for e in all_events if e.get("sensor_id") == sensor_id]
        exclusions[sensor_id] = uptime_with_exclusions(
            sensor_id, window_start, window_end, sensor_events
        )

    report = {
        "schema_version": 1,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "generated_at": now.isoformat(),
        "uptime": uptime_result.to_dict(),
        "downtime_event_count": len(downtime_events),
        "downtime_events": downtime_events,
        "uptime_with_exclusions": exclusions,
        "cause_breakdown": _cause_breakdown(downtime_events),
    }

    if write_reports:
        _write_report(
            KPI_UPTIME_DIR / f"{window_start.strftime('%Y-%m-%dT%H')}.uptime.json",
            report,
        )
    return report


def _cause_breakdown(events: list[dict]) -> dict:
    """Count + total duration of downtime by cause and by bucket."""
    by_cause: dict[str, dict] = {}
    by_bucket: dict[str, dict] = {}
    for e in events:
        cause = e.get("cause", "Unknown Downtime")
        dur = e.get("duration_seconds", 0.0)
        entry = by_cause.setdefault(cause, {"count": 0, "duration_seconds": 0.0})
        entry["count"] += 1
        entry["duration_seconds"] += dur
        bucket = cause_bucket(cause)
        b = by_bucket.setdefault(bucket, {"count": 0, "duration_seconds": 0.0})
        b["count"] += 1
        b["duration_seconds"] += dur
    return {"by_cause": by_cause, "by_bucket": by_bucket}


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with __import__("os").fdopen(fd, "w") as f:
        json.dump(report, f, indent=2)
    Path(tmp).replace(path)
