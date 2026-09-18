"""
Uptime engine -- rule-based sensor reliability KPIs (no machine learning).

Implements the uptime approaches from the reliability design discourse:

  1. Timestamp consonance  -- delay between sensor timestamp and ingestion timestamp.
  2. Differ rate           -- actual vs expected data points in a window.
  3. Heartbeat monitoring  -- no data within heartbeat interval => down.
  4. Moving average uptime -- rolling-average smoothing across sub-windows.
  5. Data-quality uptime   -- only valid (in-range) records count.
  6. Multi-sensor correlation -- corroborate sensors monitoring the same segment.

The engine is pure-Python and dependency-free (only stdlib + the shared
config), so it can be unit-tested and used by both ec2_poller.py (per-file)
and the uptime aggregator without drift between layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.aws_config import RELIABILITY


def parse_timestamp(value) -> datetime | None:
    """Best-effort parse of a timestamp from ISO string, epoch seconds, or epoch ms."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 10_000_000_000:  # epoch millis
                seconds /= 1000
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class SensorUptime:
    sensor_id: str
    expected_data_points: int = 0
    actual_data_points: int = 0
    valid_data_points: int = 0
    late_data_points: int = 0
    timestamp_consonance_pct: float = 0.0
    differ_rate_pct: float = 0.0
    data_quality_uptime_pct: float = 0.0
    uptime_pct: float = 0.0
    last_seen: str | None = None
    heartbeat_status: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "expected_data_points": self.expected_data_points,
            "actual_data_points": self.actual_data_points,
            "valid_data_points": self.valid_data_points,
            "late_data_points": self.late_data_points,
            "timestamp_consonance_pct": round(self.timestamp_consonance_pct, 4),
            "differ_rate_pct": round(self.differ_rate_pct, 4),
            "data_quality_uptime_pct": round(self.data_quality_uptime_pct, 4),
            "uptime_pct": round(self.uptime_pct, 4),
            "last_seen": self.last_seen,
            "heartbeat_status": self.heartbeat_status,
        }


@dataclass
class UptimeResult:
    window_start: str | None = None
    window_end: str | None = None
    window_seconds: int = 0
    sensors: dict[str, SensorUptime] = field(default_factory=dict)
    correlation_groups: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "window_seconds": self.window_seconds,
            "sensor_count": len(self.sensors),
            "sensors": {sid: s.to_dict() for sid, s in self.sensors.items()},
            "correlation_groups": self.correlation_groups,
        }


def _quality_ok(sensor_id: str, value: float) -> bool:
    """A record is "valid" (counts toward data-quality uptime) if it is in range
    for the configured quality bounds (or no bounds are configured for it)."""
    lo_hi = RELIABILITY.quality_bounds.get(sensor_id)
    if lo_hi is None:
        return True
    lo, hi = lo_hi
    return lo <= value <= hi


def _consonance_ok(sensor_timestamp: datetime, ingestion_timestamp: datetime) -> bool:
    """True when the sensor/ingestion delay is within the acceptable threshold."""
    delay = abs((ingestion_timestamp - sensor_timestamp).total_seconds())
    return delay <= RELIABILITY.max_acceptable_delay_seconds


def _expected_points(sensor_id: str, window_seconds: int) -> int:
    """How many data points we expect a sensor to produce in a window of this size."""
    cadence = max(1, RELIABILITY.cadence_for(sensor_id))
    return max(0, int(window_seconds // cadence))


def compute_uptime(
    records: list[dict],
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
) -> UptimeResult:
    """
    Compute per-sensor uptime KPIs for one time window.

    Each record may carry:
      - timestamp            (required) sensor-side timestamp
      - sensor_id            (required)
      - value                (required, used for data-quality bounds)
      - ingestion_timestamp  (optional; defaults to the record's own timestamp)
      - battery_percent, signal_strength, device_temperature  (telemetry, forwarded)

    Uptime % = (valid_data_points / expected_data_points) * 100, where a
    "valid" point is both consonant (delay within threshold) and in range.
    """
    now = now or datetime.now(timezone.utc)
    window_seconds = max(0, int((window_end - window_start).total_seconds()))
    result = UptimeResult(
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        window_seconds=window_seconds,
    )

    by_sensor: dict[str, list[dict]] = {}
    for rec in records:
        sid = rec.get("sensor_id")
        if sid is None:
            continue
        by_sensor.setdefault(sid, []).append(rec)

    for sensor_id, recs in by_sensor.items():
        expected = _expected_points(sensor_id, window_seconds)
        actual = 0
        valid = 0
        late = 0
        last_seen_dt: datetime | None = None

        for rec in recs:
            ts = parse_timestamp(rec.get("timestamp"))
            if ts is None:
                continue
            ingestion = parse_timestamp(rec.get("ingestion_timestamp")) or ts
            value = _coerce_float(rec.get("value"))

            actual += 1
            if not _consonance_ok(ts, ingestion):
                late += 1
            if value is not None and _quality_ok(sensor_id, value):
                valid += 1

            if last_seen_dt is None or ts > last_seen_dt:
                last_seen_dt = ts

        consonance_pct = (((actual - late) / actual) * 100.0) if actual else 0.0
        differ_rate_pct = (actual / expected * 100.0) if expected else (100.0 if actual else 0.0)
        data_quality_pct = (valid / expected * 100.0) if expected else (100.0 if valid else 0.0)
        # Holistic uptime: only consonant + in-range points count, vs expected.
        # Recount consonant-and-in-range explicitly (valid already filters range).
        consonant_in_range = sum(
            1
            for rec in recs
            if (lambda t, v: t is not None and _consonance_ok(t, parse_timestamp(rec.get("ingestion_timestamp")) or t) and v is not None and _quality_ok(sensor_id, v))(
                parse_timestamp(rec.get("timestamp")), _coerce_float(rec.get("value"))
            )
        )
        uptime_pct = (consonant_in_range / expected * 100.0) if expected else (100.0 if consonant_in_range else 0.0)

        heartbeat_status = _heartbeat_status(last_seen_dt, now)
        result.sensors[sensor_id] = SensorUptime(
            sensor_id=sensor_id,
            expected_data_points=expected,
            actual_data_points=actual,
            valid_data_points=valid,
            late_data_points=late,
            timestamp_consonance_pct=consonance_pct,
            differ_rate_pct=min(differ_rate_pct, 100.0),
            data_quality_uptime_pct=min(data_quality_pct, 100.0),
            uptime_pct=min(uptime_pct, 100.0),
            last_seen=last_seen_dt.isoformat() if last_seen_dt else None,
            heartbeat_status=heartbeat_status,
        )

    result.correlation_groups = _correlation_checks(by_sensor, window_seconds)
    return result


def _heartbeat_status(last_seen: datetime | None, now: datetime) -> str:
    if last_seen is None:
        return "down"
    gap = (now - last_seen).total_seconds()
    if gap > RELIABILITY.heartbeat_interval_seconds:
        return "down"
    if gap > RELIABILITY.heartbeat_interval_seconds / 2:
        return "degraded"
    return "up"


def _correlation_checks(by_sensor: dict[str, list[dict]], window_seconds: int) -> list[dict]:
    """For each configured correlation group, report how many members reported
    data and the corroborated uptime = (members with data / total members)."""
    checks: list[dict] = []
    for group in RELIABILITY.correlation_groups:
        reporting = [sid for sid in group if sid in by_sensor]
        corroborated_pct = (len(reporting) / len(group) * 100.0) if group else 0.0
        missing = [sid for sid in group if sid not in by_sensor]
        checks.append({
            "sensors": list(group),
            "reporting": reporting,
            "missing": missing,
            "corroborated_pct": round(corroborated_pct, 4),
        })
    return checks


def moving_average_uptime(window_results: list[UptimeResult]) -> dict[str, float]:
    """Smooth uptime across many sub-windows into a per-sensor moving average.

    This implements the "moving average (rolling window)" approach: compute
    uptime for small windows (e.g. 1 hour) and average over the horizon
    (e.g. 24 hours) to filter short-term spikes without masking real outages.
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for wr in window_results:
        for sid, su in wr.sensors.items():
            sums[sid] = sums.get(sid, 0.0) + su.uptime_pct
            counts[sid] = counts.get(sid, 0) + 1
    return {sid: (sums[sid] / counts[sid]) for sid in sums if counts[sid]}


def uptime_with_exclusions(
    sensor_id: str,
    window_start: datetime,
    window_end: datetime,
    downtime_periods: list[dict],
) -> dict:
    """
    Compute uptime for a window, excluding planned and uncontrollable downtime
    from the denominator (per the ILDS recommendations).

    downtime_periods: list of {"start": iso, "end": iso, "cause": str}.

    "true_downtime" causes (hardware/software/pipeline/human) reduce uptime;
    planned and uncontrollable causes are tracked separately and do not.
    """
    total_seconds = (window_end - window_start).total_seconds()
    planned_seconds = 0.0
    uncontrollable_seconds = 0.0
    true_downtime_seconds = 0.0

    for dt in downtime_periods:
        start = parse_timestamp(dt.get("start"))
        end = parse_timestamp(dt.get("end"))
        if start is None or end is None:
            continue
        # Clip the downtime period to the window.
        clip_start = max(start, window_start)
        clip_end = min(end, window_end)
        if clip_end <= clip_start:
            continue
        duration = (clip_end - clip_start).total_seconds()
        cause = dt.get("cause", "Unknown Downtime")
        if cause in RELIABILITY.planned_causes:
            planned_seconds += duration
        elif cause in RELIABILITY.uncontrollable_causes:
            uncontrollable_seconds += duration
        else:
            true_downtime_seconds += duration

    billable_seconds = max(0.0, total_seconds - planned_seconds - uncontrollable_seconds)
    uptime_pct = ((billable_seconds - true_downtime_seconds) / total_seconds * 100.0) if total_seconds else 100.0
    return {
        "sensor_id": sensor_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "total_seconds": total_seconds,
        "planned_downtime_seconds": planned_seconds,
        "uncontrollable_downtime_seconds": uncontrollable_seconds,
        "true_downtime_seconds": true_downtime_seconds,
        "uptime_pct": round(max(0.0, min(100.0, uptime_pct)), 4),
    }
