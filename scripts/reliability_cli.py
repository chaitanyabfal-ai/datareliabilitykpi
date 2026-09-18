#!/usr/bin/env python3
"""
reliability_cli.py -- standalone, offline entry point for the data-reliability
KPIs (uptime/downtime). Computes the rule-based reliability metrics directly
from a local sensor file (CSV or JSON) without needing AWS/SQS, and writes the
same report shape the poller and aggregator produce:

    -> data/kpi_reports/uptime/<timestamp>.uptime.json
    -> data/downtime/downtime_events.jsonl

This is the "standalone implementation of uptime/downtime KPI metrics" entry
point: it lets you validate the reliability logic against a sample file before
wiring it into the full S3 -> SQS -> EC2 pipeline.

Usage:
    python3 scripts/reliability_cli.py path/to/sensor_file.json
    python3 scripts/reliability_cli.py path/to/sensor_file.csv --window-minutes 60
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.aws_config import KPI_UPTIME_DIR, LOG_DIR  # noqa: E402
from scripts.utils.downtime_classifier import build_default_classifier  # noqa: E402
from scripts.utils.downtime_event_log import DowntimeEventLog  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.reliability_reporter import report_reliability  # noqa: E402
from scripts.utils.schema import validate_bytes  # noqa: E402
from scripts.utils.uptime_engine import parse_timestamp  # noqa: E402

logger = get_logger("reliability_cli", LOG_DIR / "reliability_cli.log")


def _window_from_records(records: list[dict], window_minutes: int | None):
    timestamps = [parse_timestamp(r.get("timestamp")) for r in records]
    timestamps = [t for t in timestamps if t is not None]
    if not timestamps:
        now = datetime.now(timezone.utc)
        end = now
        start = now.fromtimestamp(now.timestamp() - (window_minutes or 60) * 60, tz=timezone.utc)
        return start, end
    start = min(timestamps)
    end = max(timestamps)
    if window_minutes:
        from datetime import timedelta
        end = max(end, start + timedelta(minutes=window_minutes))
    if end <= start:
        end = start
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Local sensor file (.json or .csv) to analyze.")
    parser.add_argument(
        "--window-minutes", type=int, default=None,
        help="Force the reliability window length in minutes (else derived from file timestamps).",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Print the report instead of writing it to data/kpi_reports/uptime/.",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    records, validation = validate_bytes(path.read_bytes(), path.name)
    if not validation.ok:
        print(f"Schema validation failed: {validation.errors[:5]}", file=sys.stderr)
        return 1

    start, end = _window_from_records(records, args.window_minutes)
    report = report_reliability(
        records=records,
        window_start=start,
        window_end=end,
        classifier=build_default_classifier(),
        event_log=DowntimeEventLog(),
        write_reports=not args.no_write,
    )

    if args.no_write:
        print(json.dumps(report, indent=2))
    else:
        out = KPI_UPTIME_DIR / f"{start.strftime('%Y-%m-%dT%H%M%S')}.uptime.json"
        with open(out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Reliability report written to {out}")
        print(
            f"Sensors: {report['uptime']['sensor_count']}  "
            f"Downtime events: {report['downtime_event_count']}  "
            f"Causes: {list(report['cause_breakdown']['by_cause'].keys()) or 'none'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
