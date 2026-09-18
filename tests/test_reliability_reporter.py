"""Tests for the reliability reporter (downtime gap detection + report wiring)."""
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.utils.reliability_reporter as rr  # noqa: E402
import scripts.utils.uptime_engine as ue  # noqa: E402
from scripts.utils.downtime_classifier import DowntimeClassifier, Detection  # noqa: E402
from scripts.utils.downtime_event_log import DowntimeEventLog  # noqa: E402


def _patch_reliability(monkeypatch, **overrides):
    base = replace(ue.RELIABILITY, **overrides)
    monkeypatch.setattr(ue, "RELIABILITY", base)
    monkeypatch.setattr(rr, "RELIABILITY", base)
    return base


def _dt(minute=0):
    return datetime(2026, 9, 11, 8, minute, tzinfo=timezone.utc)


def test_detect_downtime_gaps_finds_missing_window(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        heartbeat_interval_seconds=600,
        quality_bounds={},
    )
    # Sensor reports at 08:00 and 08:20 -- gap of 1200s > heartbeat 600s.
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "BFA8", "value": 3.0},
        {"timestamp": "2026-09-11T08:20:00Z", "sensor_id": "BFA8", "value": 3.1},
    ]
    gaps = rr.detect_downtime_gaps("BFA8", records, _dt(0), _dt(30))
    assert len(gaps) == 1
    start, end = gaps[0]
    assert start == _dt(0)
    assert end == _dt(20)


def test_detect_downtime_gaps_none_when_continuous(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        heartbeat_interval_seconds=600,
        quality_bounds={},
    )
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "BFA8", "value": 3.0},
        {"timestamp": "2026-09-11T08:05:00Z", "sensor_id": "BFA8", "value": 3.1},
        {"timestamp": "2026-09-11T08:10:00Z", "sensor_id": "BFA8", "value": 3.2},
    ]
    assert rr.detect_downtime_gaps("BFA8", records, _dt(0), _dt(15)) == []


def test_report_reliability_classifies_and_logs(monkeypatch, tmp_path):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        max_acceptable_delay_seconds=600,
        heartbeat_interval_seconds=600,
        quality_bounds={},
        correlation_groups=[],
    )
    # Force a gap by reporting at 08:00 and 08:20.
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "BFA8", "value": 3.0},
        {"timestamp": "2026-09-11T08:20:00Z", "sensor_id": "BFA8", "value": 3.1},
    ]
    log = DowntimeEventLog(tmp_path / "events.jsonl")
    clf = DowntimeClassifier([("hardware", lambda *_: Detection(cause="Sensor Malfunction", category="hardware"))])

    # Avoid writing to the real report dir; patch the writer to no-op.
    monkeypatch.setattr(rr, "_write_report", lambda path, report: None)

    report = rr.report_reliability(
        records=records,
        window_start=_dt(0),
        window_end=_dt(30),
        classifier=clf,
        event_log=log,
        write_reports=False,
    )
    assert report["downtime_event_count"] >= 1
    assert report["downtime_events"][0]["cause"] == "Sensor Malfunction"
    # The event log should now contain the classified event.
    assert len(log.load()) >= 1
    assert log.load()[0]["cause"] == "Sensor Malfunction"


def test_downtime_event_log_append_and_load(tmp_path):
    log = DowntimeEventLog(tmp_path / "log.jsonl")
    log.append({"sensor_id": "BFA8", "cause": "Airtel LTE Outage"})
    log.append({"sensor_id": "BFA3", "cause": "Pigging Operation"})
    events = log.load()
    assert len(events) == 2
    assert log.load_for_sensor("BFA8")[0]["cause"] == "Airtel LTE Outage"
