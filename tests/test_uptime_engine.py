"""Tests for the rule-based uptime engine (no ML)."""
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.utils.uptime_engine as ue  # noqa: E402


def _patch_reliability(monkeypatch, **overrides):
    base = replace(ue.RELIABILITY, **overrides)
    monkeypatch.setattr(ue, "RELIABILITY", base)
    return base


def test_parse_timestamp_iso_epoch_and_ms():
    assert ue.parse_timestamp("2026-09-11T08:00:00Z").year == 2026
    assert ue.parse_timestamp(1777461880).year == 2026
    assert ue.parse_timestamp(1777461880000).year == 2026  # epoch ms
    assert ue.parse_timestamp(None) is None
    assert ue.parse_timestamp("not a date") is None


def test_timestamp_consonance_marks_late_points(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        max_acceptable_delay_seconds=60,
        heartbeat_interval_seconds=600,
        quality_bounds={},
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "s1", "value": 1, "ingestion_timestamp": "2026-09-11T08:00:05Z"},
        {"timestamp": "2026-09-11T08:05:00Z", "sensor_id": "s1", "value": 2, "ingestion_timestamp": "2026-09-11T08:07:00Z"},  # 120s late
    ]
    result = ue.compute_uptime(records, start, end, now=end)
    s = result.sensors["s1"]
    assert s.actual_data_points == 2
    assert s.late_data_points == 1
    assert s.timestamp_consonance_pct == 50.0


def test_differ_rate_vs_expected(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,  # 5 min => 4 expected in 20 min
        max_acceptable_delay_seconds=600,
        heartbeat_interval_seconds=600,
        quality_bounds={},
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "s1", "value": 1},
        {"timestamp": "2026-09-11T08:05:00Z", "sensor_id": "s1", "value": 2},
    ]
    result = ue.compute_uptime(records, start, end, now=end)
    s = result.sensors["s1"]
    assert s.expected_data_points == 4
    assert s.actual_data_points == 2
    assert s.differ_rate_pct == 50.0


def test_data_quality_uptime_excludes_out_of_range(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        max_acceptable_delay_seconds=600,
        heartbeat_interval_seconds=600,
        quality_bounds={"s1": (0.0, 100.0)},
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "s1", "value": 50},
        {"timestamp": "2026-09-11T08:05:00Z", "sensor_id": "s1", "value": 120},  # out of range
        {"timestamp": "2026-09-11T08:10:00Z", "sensor_id": "s1", "value": 60},
        {"timestamp": "2026-09-11T08:15:00Z", "sensor_id": "s1", "value": 70},
    ]
    result = ue.compute_uptime(records, start, end, now=end)
    s = result.sensors["s1"]
    assert s.valid_data_points == 3  # 120 excluded
    # 3 valid of 4 expected
    assert s.data_quality_uptime_pct == 75.0


def test_heartbeat_status_down_when_no_recent_data(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        max_acceptable_delay_seconds=600,
        heartbeat_interval_seconds=600,
        quality_bounds={},
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    records = [{"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "s1", "value": 1}]
    # 'now' well past the heartbeat interval
    now = end + timedelta(hours=1)
    result = ue.compute_uptime(records, start, end, now=now)
    assert result.sensors["s1"].heartbeat_status == "down"


def test_moving_average_uptime_smooths_windows(monkeypatch):
    _patch_reliability(monkeypatch, quality_bounds={})
    w1 = ue.UptimeResult()
    w1.sensors["s1"] = ue.SensorUptime(sensor_id="s1", uptime_pct=80.0)
    w2 = ue.UptimeResult()
    w2.sensors["s1"] = ue.SensorUptime(sensor_id="s1", uptime_pct=100.0)
    ma = ue.moving_average_uptime([w1, w2])
    assert ma["s1"] == 90.0


def test_correlation_groups_report_missing_sensors(monkeypatch):
    _patch_reliability(
        monkeypatch,
        expected_frequency_seconds=300,
        max_acceptable_delay_seconds=600,
        heartbeat_interval_seconds=600,
        quality_bounds={},
        correlation_groups=[["BFA8", "BFA3"]],
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    records = [{"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "BFA8", "value": 3.0}]
    result = ue.compute_uptime(records, start, end, now=end)
    assert result.correlation_groups
    cg = result.correlation_groups[0]
    assert cg["reporting"] == ["BFA8"]
    assert cg["missing"] == ["BFA3"]
    assert cg["corroborated_pct"] == 50.0


def test_uptime_with_exclusions_separates_planned_and_true_downtime(monkeypatch):
    _patch_reliability(
        monkeypatch,
        planned_causes=("Pigging Operation",),
        uncontrollable_causes=("Airtel LTE Outage",),
    )
    start = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    # 10 min pigging (planned), 10 min LTE outage (uncontrollable), 10 min sensor malfunction (true)
    downtimes = [
        {"start": (start + timedelta(minutes=10)).isoformat(), "end": (start + timedelta(minutes=20)).isoformat(), "cause": "Pigging Operation"},
        {"start": (start + timedelta(minutes=20)).isoformat(), "end": (start + timedelta(minutes=30)).isoformat(), "cause": "Airtel LTE Outage"},
        {"start": (start + timedelta(minutes=30)).isoformat(), "end": (start + timedelta(minutes=40)).isoformat(), "cause": "Sensor Malfunction"},
    ]
    out = ue.uptime_with_exclusions("BFA8", start, end, downtimes)
    assert out["planned_downtime_seconds"] == 600
    assert out["uncontrollable_downtime_seconds"] == 600
    assert out["true_downtime_seconds"] == 600
    # billable = 3600 - 600 - 600 = 2400; uptime = (2400 - 600)/3600
    assert abs(out["uptime_pct"] - 50.0) < 0.01
