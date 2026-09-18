"""
Shared KPI computation engine.

Used by both ec2_poller.py (per-file windows) and kpi_aggregator.py
(hourly/daily rollups of those windows) so numbers never drift between
layers -- the aggregator re-derives hourly/daily stats from the same
per-sensor sums rather than averaging pre-computed averages.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class SensorStats:
    """Running/finalized statistics for a single sensor_id."""

    sensor_id: str
    count: int = 0
    total: float = 0.0
    min_value: float = math.inf
    max_value: float = -math.inf
    sum_sq: float = 0.0
    first_value: float | None = None
    last_value: float | None = None
    threshold_breaches: int = 0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.sum_sq += value * value
        self.min_value = min(self.min_value, value)
        self.max_value = max(self.max_value, value)
        if self.first_value is None:
            self.first_value = value
        self.last_value = value

    def merge(self, other: "SensorStats") -> None:
        if other.count == 0:
            return
        self.count += other.count
        self.total += other.total
        self.sum_sq += other.sum_sq
        self.min_value = min(self.min_value, other.min_value)
        self.max_value = max(self.max_value, other.max_value)
        self.threshold_breaches += other.threshold_breaches
        if self.first_value is None:
            self.first_value = other.first_value
        self.last_value = other.last_value

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    @property
    def std(self) -> float | None:
        if self.count < 2:
            return 0.0 if self.count == 1 else None
        variance = (self.sum_sq / self.count) - (self.mean ** 2)
        return math.sqrt(max(variance, 0.0))

    @property
    def rate_of_change(self) -> float | None:
        if self.first_value is None or self.last_value is None or self.count < 2:
            return None
        return self.last_value - self.first_value

    def to_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "count": self.count,
            "mean": round(self.mean, 6) if self.mean is not None else None,
            "min": self.min_value if self.count else None,
            "max": self.max_value if self.count else None,
            "std": round(self.std, 6) if self.std is not None else None,
            "rate_of_change": round(self.rate_of_change, 6) if self.rate_of_change is not None else None,
            "threshold_breaches": self.threshold_breaches,
        }


@dataclass
class KPIResult:
    sensors: dict[str, SensorStats] = field(default_factory=dict)
    total_records: int = 0
    invalid_records: int = 0

    def to_dict(self) -> dict:
        return {
            "total_records": self.total_records,
            "invalid_records": self.invalid_records,
            "sensor_count": len(self.sensors),
            "sensors": {sid: s.to_dict() for sid, s in self.sensors.items()},
        }


def compute_kpis(records: list[dict], thresholds: dict[str, tuple[float, float]] | None = None) -> KPIResult:
    """
    Compute per-sensor KPI stats for a list of {"sensor_id", "value", ...} records.

    `thresholds` maps sensor_id (or field name) -> (min, max); values outside
    the range count as a threshold breach for that sensor.
    """
    thresholds = thresholds or {}
    result = KPIResult()

    for rec in records:
        result.total_records += 1
        sensor_id = rec.get("sensor_id")
        raw_value = rec.get("value")
        if sensor_id is None or raw_value is None:
            result.invalid_records += 1
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            result.invalid_records += 1
            continue

        stats = result.sensors.setdefault(sensor_id, SensorStats(sensor_id=sensor_id))
        stats.add(value)

        lo_hi = thresholds.get(sensor_id)
        if lo_hi is not None:
            lo, hi = lo_hi
            if value < lo or value > hi:
                stats.threshold_breaches += 1

    return result


def merge_kpi_results(results: list[KPIResult]) -> KPIResult:
    """Combine several KPIResult objects (e.g. many per-file windows) into one."""
    merged = KPIResult()
    for r in results:
        merged.total_records += r.total_records
        merged.invalid_records += r.invalid_records
        for sensor_id, stats in r.sensors.items():
            target = merged.sensors.setdefault(sensor_id, SensorStats(sensor_id=sensor_id))
            target.merge(stats)
    return merged
