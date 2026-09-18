#!/usr/bin/env python3
"""
kpi_aggregator.py -- rolls the per-file window reports written by
ec2_poller.py up into hourly and daily summaries for the dashboard, and
also rolls the per-file reliability (uptime/downtime) reports up into
hourly and daily reliability summaries.

    data/kpi_reports/windows/*.json
        -> data/kpi_reports/hourly/<YYYY-MM-DD-HH>.json
        -> data/kpi_reports/daily/<YYYY-MM-DD>.json

    data/kpi_reports/uptime/*.uptime.json
        -> data/kpi_reports/uptime/hourly/<YYYY-MM-DD-HH>.uptime.json
        -> data/kpi_reports/uptime/daily/<YYYY-MM-DD>.uptime.json

Run once (`--once`, e.g. from cron every few minutes) or continuously
(`--watch`, rebuilding on an interval).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.aws_config import (  # noqa: E402
    KPI_DAILY_DIR, KPI_HOURLY_DIR, KPI_UPTIME_DIR, KPI_WINDOWS_DIR, LOG_DIR, RELIABILITY,
)
from scripts.utils.kpi_engine import KPIResult, SensorStats, merge_kpi_results  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.uptime_engine import moving_average_uptime  # noqa: E402

logger = get_logger("kpi_aggregator", LOG_DIR / "kpi_aggregator.log")

UPTIME_HOURLY_DIR = KPI_UPTIME_DIR / "hourly"
UPTIME_DAILY_DIR = KPI_UPTIME_DIR / "daily"


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _load_window_reports() -> list[dict]:
    reports = []
    for path in sorted(KPI_WINDOWS_DIR.glob("*.json")):
        data = _load_json(path)
        if data:
            reports.append(data)
    return reports


def _load_uptime_reports() -> list[dict]:
    reports = []
    for path in sorted(KPI_UPTIME_DIR.glob("*.uptime.json")):
        data = _load_json(path)
        if data:
            reports.append(data)
    return reports


def _kpi_result_from_window(window: dict) -> KPIResult:
    result = KPIResult(total_records=window.get("total_records", 0), invalid_records=window.get("invalid_records", 0))
    for sensor_id, s in window.get("sensors", {}).items():
        stats = SensorStats(
            sensor_id=sensor_id,
            count=s.get("count", 0),
            total=(s.get("mean") or 0) * s.get("count", 0),
            min_value=s.get("min", float("inf")) if s.get("min") is not None else float("inf"),
            max_value=s.get("max", float("-inf")) if s.get("max") is not None else float("-inf"),
            sum_sq=((s.get("std") or 0) ** 2 + (s.get("mean") or 0) ** 2) * s.get("count", 0),
            threshold_breaches=s.get("threshold_breaches", 0),
        )
        result.sensors[sensor_id] = stats
    return result


def _bucket_key(iso: str, granularity: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if granularity == "hourly":
        return dt.strftime("%Y-%m-%d-%H")
    return dt.strftime("%Y-%m-%d")


def _write_rollup(out_dir: Path, bucket: str, result: KPIResult, window_count: int, granularity: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "granularity": granularity,
        "bucket": bucket,
        "window_count": window_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result.to_dict(),
    }
    path = out_dir / f"{bucket}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    tmp.rename(path)


def aggregate_kpis_once() -> tuple[int, int]:
    """Hourly + daily rollups of the base per-file KPI windows."""
    windows = _load_window_reports()
    if not windows:
        logger.info("No window reports found yet -- nothing to aggregate.")
        return 0, 0

    hourly_groups: dict[str, list[dict]] = defaultdict(list)
    daily_groups: dict[str, list[dict]] = defaultdict(list)
    for w in windows:
        processed_at = w.get("processed_at")
        if not processed_at:
            continue
        hk = _bucket_key(processed_at, "hourly")
        dk = _bucket_key(processed_at, "daily")
        if hk:
            hourly_groups[hk].append(w)
        if dk:
            daily_groups[dk].append(w)

    for bucket, group in hourly_groups.items():
        merged = merge_kpi_results([_kpi_result_from_window(w) for w in group])
        _write_rollup(KPI_HOURLY_DIR, bucket, merged, len(group), "hourly")
    for bucket, group in daily_groups.items():
        merged = merge_kpi_results([_kpi_result_from_window(w) for w in group])
        _write_rollup(KPI_DAILY_DIR, bucket, merged, len(group), "daily")

    logger.info(
        "Aggregated %d KPI window(s) into %d hourly and %d daily bucket(s)",
        len(windows), len(hourly_groups), len(daily_groups),
    )
    return len(hourly_groups), len(daily_groups)


def _merge_uptime_reports(reports: list[dict]) -> dict:
    """Merge several per-window uptime reports into one reliability rollup.

    Sums expected/actual/valid/late counts per sensor, recomputes the
    derived percentages, and averages the rolling-horizon moving average.
    """
    sensors: dict[str, dict] = defaultdict(lambda: {
        "expected_data_points": 0, "actual_data_points": 0,
        "valid_data_points": 0, "late_data_points": 0,
        "uptime_pct_sum": 0.0, "uptime_pct_count": 0,
        "last_seen": None, "heartbeat_status": "unknown",
    })
    correlation_groups: list[dict] = []
    for rep in reports:
        for sid, su in rep.get("uptime", {}).get("sensors", {}).items():
            agg = sensors[sid]
            agg["expected_data_points"] += su.get("expected_data_points", 0)
            agg["actual_data_points"] += su.get("actual_data_points", 0)
            agg["valid_data_points"] += su.get("valid_data_points", 0)
            agg["late_data_points"] += su.get("late_data_points", 0)
            agg["uptime_pct_sum"] += su.get("uptime_pct", 0.0)
            agg["uptime_pct_count"] += 1
            ls = su.get("last_seen")
            if ls and (agg["last_seen"] is None or ls > agg["last_seen"]):
                agg["last_seen"] = ls
                agg["heartbeat_status"] = su.get("heartbeat_status", "unknown")
        for cg in rep.get("uptime", {}).get("correlation_groups", []):
            correlation_groups.append(cg)

    out_sensors = {}
    for sid, agg in sensors.items():
        actual = agg["actual_data_points"]
        expected = agg["expected_data_points"]
        late = agg["late_data_points"]
        valid = agg["valid_data_points"]
        consonance = (((actual - late) / actual) * 100.0) if actual else 0.0
        differ = (actual / expected * 100.0) if expected else (100.0 if actual else 0.0)
        quality = (valid / expected * 100.0) if expected else (100.0 if valid else 0.0)
        uptime = (agg["uptime_pct_sum"] / agg["uptime_pct_count"]) if agg["uptime_pct_count"] else 0.0
        out_sensors[sid] = {
            "sensor_id": sid,
            "expected_data_points": expected,
            "actual_data_points": actual,
            "valid_data_points": valid,
            "late_data_points": late,
            "timestamp_consonance_pct": round(consonance, 4),
            "differ_rate_pct": round(min(differ, 100.0), 4),
            "data_quality_uptime_pct": round(min(quality, 100.0), 4),
            "uptime_pct": round(min(uptime, 100.0), 4),
            "last_seen": agg["last_seen"],
            "heartbeat_status": agg["heartbeat_status"],
        }

    total_events = sum(rep.get("downtime_event_count", 0) for rep in reports)
    all_events = [e for rep in reports for e in rep.get("downtime_events", [])]
    by_cause: dict[str, dict] = defaultdict(lambda: {"count": 0, "duration_seconds": 0.0})
    for e in all_events:
        c = e.get("cause", "Unknown Downtime")
        by_cause[c]["count"] += 1
        by_cause[c]["duration_seconds"] += e.get("duration_seconds", 0.0)

    return {
        "sensor_count": len(out_sensors),
        "sensors": out_sensors,
        "correlation_groups": correlation_groups,
        "downtime_event_count": total_events,
        "downtime_by_cause": {k: dict(v) for k, v in by_cause.items()},
    }


def aggregate_reliability_once() -> tuple[int, int]:
    """Hourly + daily rollups of the per-file reliability (uptime/downtime) reports."""
    reports = _load_uptime_reports()
    if not reports:
        return 0, 0

    hourly_groups: dict[str, list[dict]] = defaultdict(list)
    daily_groups: dict[str, list[dict]] = defaultdict(list)
    for rep in reports:
        ws = rep.get("window_start")
        if not ws:
            continue
        hk = _bucket_key(ws, "hourly")
        dk = _bucket_key(ws, "daily")
        if hk:
            hourly_groups[hk].append(rep)
        if dk:
            daily_groups[dk].append(rep)

    def _write(out_dir, bucket, group, granularity):
        out_dir.mkdir(parents=True, exist_ok=True)
        merged = _merge_uptime_reports(group)
        payload = {
            "schema_version": 1,
            "granularity": granularity,
            "bucket": bucket,
            "window_count": len(group),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **merged,
        }
        path = out_dir / f"{bucket}.uptime.json"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.rename(path)

    for bucket, group in hourly_groups.items():
        _write(UPTIME_HOURLY_DIR, bucket, group, "hourly")
    for bucket, group in daily_groups.items():
        _write(UPTIME_DAILY_DIR, bucket, group, "daily")

    # Moving-average uptime across the configured rolling horizon (e.g. 24h).
    if reports:
        from scripts.utils.uptime_engine import UptimeResult, SensorUptime
        # Reconstruct minimal UptimeResult objects for the moving-average helper.
        window_results = []
        for rep in reports:
            ur = UptimeResult()
            for sid, su in rep.get("uptime", {}).get("sensors", {}).items():
                ur.sensors[sid] = SensorUptime(
                    sensor_id=sid, uptime_pct=su.get("uptime_pct", 0.0)
                )
            window_results.append(ur)
        ma = moving_average_uptime(window_results)
        horizon_path = KPI_UPTIME_DIR / f"moving_average_{RELIABILITY.uptime_rolling_horizon_hours}h.json"
        with open(horizon_path, "w") as f:
            json.dump(
                {
                    "schema_version": 1,
                    "horizon_hours": RELIABILITY.uptime_rolling_horizon_hours,
                    "window_count": len(reports),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "moving_average_uptime_pct": {k: round(v, 4) for k, v in ma.items()},
                },
                f, indent=2,
            )

    logger.info(
        "Aggregated %d reliability report(s) into %d hourly and %d daily bucket(s)",
        len(reports), len(hourly_groups), len(daily_groups),
    )
    return len(hourly_groups), len(daily_groups)


def aggregate_once() -> tuple[int, int]:
    n_hourly, n_daily = aggregate_kpis_once()
    aggregate_reliability_once()
    return n_hourly, n_daily


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Re-aggregate on an interval instead of exiting.")
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds between re-aggregation passes in --watch mode.")
    args = parser.parse_args()

    if args.watch:
        logger.info("Starting continuous aggregation (interval=%.1fs)", args.interval)
        try:
            while True:
                aggregate_once()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Shutting down on Ctrl-C")
    else:
        aggregate_once()


if __name__ == "__main__":
    main()
