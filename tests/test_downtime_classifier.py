"""Tests for the rule-based downtime classifier + detectors (no ML)."""
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.utils.downtime_classifier as dc  # noqa: E402
import scripts.utils.detectors.detectors as det  # noqa: E402
from scripts.utils.maintenance_calendar import MaintenanceCalendar  # noqa: E402


def _patch_reliability(monkeypatch, **overrides):
    base = replace(det.RELIABILITY, **overrides)
    monkeypatch.setattr(det, "RELIABILITY", base)
    monkeypatch.setattr(dc, "RELIABILITY", base)
    return base


def _dt(minutes=0):
    return datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_classifier_walks_priority_order_network_first():
    # A network detector and a hardware detector both fire -> network wins.
    def net(_sid, _s, _e, _ctx):
        return dc.Detection(cause="Airtel LTE Outage", category="network")

    def hw(_sid, _s, _e, _ctx):
        return dc.Detection(cause="Sensor Malfunction", category="hardware")

    clf = dc.DowntimeClassifier([("hardware", hw), ("network", net)])
    result = clf.classify("BFA8", _dt(), _dt(10))
    assert result.cause == "Airtel LTE Outage"
    assert result.category == "network"


def test_classifier_returns_unknown_when_no_detector_matches():
    clf = dc.DowntimeClassifier([("network", lambda *_: None)])
    result = clf.classify("BFA8", _dt(), _dt(10))
    assert result.cause == "Unknown Downtime"
    assert result.category == "unknown"


def test_hardware_detector_battery_depletion(monkeypatch):
    _patch_reliability(monkeypatch, min_battery_percent=20.0, min_signal_strength=10.0)
    ctx = {"telemetry": {"BFA8": {"battery_percent": 5.0, "signal_strength": 18}}}
    det_res = det.hardware_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "Battery Depletion"
    assert det_res.category == "hardware"


def test_hardware_detector_power_failure_when_battery_zero(monkeypatch):
    _patch_reliability(monkeypatch, min_battery_percent=20.0, min_signal_strength=10.0)
    ctx = {"telemetry": {"BFA8": {"battery_percent": 0.0}}}
    det_res = det.hardware_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "Power Failure"


def test_hardware_detector_sim_issue(monkeypatch):
    _patch_reliability(monkeypatch, min_battery_percent=20.0, min_signal_strength=10.0)
    ctx = {"telemetry": {"BFA8": {"signal_strength": 3.0}}}
    det_res = det.hardware_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "SIM Card Issue"


def test_hardware_detector_returns_none_when_healthy(monkeypatch):
    _patch_reliability(monkeypatch, min_battery_percent=20.0, min_signal_strength=10.0)
    ctx = {"telemetry": {"BFA8": {"battery_percent": 90.0, "signal_strength": 18.0}}}
    assert det.hardware_detector("BFA8", _dt(), _dt(10), ctx) is None


def test_software_detector_schema_validation(monkeypatch):
    _patch_reliability(monkeypatch)
    ctx = {"schema_errors": 7}
    det_res = det.software_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "Schema Validation Failure"
    assert det_res.category == "software"


def test_operational_detector_matches_maintenance_calendar(tmp_path):
    cal = MaintenanceCalendar(tmp_path / "maintenance.json")
    cal.add_window(_dt(10), _dt(20), "Pigging Operation", sensor_ids=["BFA8"])
    ctx = {"maintenance_calendar": cal}
    det_res = det.operational_detector("BFA8", _dt(12), _dt(18), ctx)
    assert det_res.cause == "Pigging Operation"
    assert det_res.category == "operational"


def test_operational_detector_no_match_returns_none(tmp_path):
    cal = MaintenanceCalendar(tmp_path / "maintenance.json")
    ctx = {"maintenance_calendar": cal}
    assert det.operational_detector("BFA8", _dt(), _dt(10), ctx) is None


def test_environmental_detector_temperature_extremes(monkeypatch):
    _patch_reliability(monkeypatch)
    ctx = {"telemetry": {"BFA8": {"device_temperature": 70.0}}}
    det_res = det.environmental_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "Temperature Extremes"
    assert det_res.category == "environmental"


def test_cybersecurity_detector_ddos(monkeypatch):
    _patch_reliability(monkeypatch)
    ctx = {"security": {"ddos": True}}
    det_res = det.cybersecurity_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "DDoS Attack"
    assert det_res.category == "cybersecurity"


def test_pipeline_detector_sqs_backlog(monkeypatch):
    _patch_reliability(monkeypatch, sqs_backlog_threshold=1000)
    ctx = {"pipeline_health": {"sqs_backlog": 5000}}
    det_res = det.pipeline_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "SQS Backlog"
    assert det_res.category == "pipeline"


def test_human_error_detector_manual_override(monkeypatch):
    _patch_reliability(monkeypatch)
    ctx = {"human_error": {"manual_override": True}}
    det_res = det.human_error_detector("BFA8", _dt(), _dt(10), ctx)
    assert det_res.cause == "Manual Override"
    assert det_res.category == "human"


def test_airtel_detector_disabled_by_default(monkeypatch):
    _patch_reliability(monkeypatch, airtel_outage_api_enabled=False)
    assert det.airtel_outage_detector("BFA8", _dt(), _dt(10), {}) is None


def test_connectivity_detector_uses_context_verdict(monkeypatch):
    _patch_reliability(monkeypatch)
    ctx = {"connectivity": {"ok": False, "reason": "VPN down"}}
    det_res = det.connectivity_detector("BFA8", _dt(), _dt(10), ctx)
    assert "VPN down" in det_res.cause
    assert det_res.category == "network"


def test_cause_bucket_classification(monkeypatch):
    _patch_reliability(
        monkeypatch,
        planned_causes=("Pigging Operation",),
        uncontrollable_causes=("Airtel LTE Outage",),
    )
    assert dc.cause_bucket("Pigging Operation") == "planned"
    assert dc.cause_bucket("Airtel LTE Outage") == "uncontrollable"
    assert dc.cause_bucket("Sensor Malfunction") == "true_downtime"
    assert dc.cause_bucket("Unknown Downtime") == "unknown"


def test_classify_downtime_event_with_string_timestamps(tmp_path, monkeypatch):
    _patch_reliability(monkeypatch, min_battery_percent=20.0, min_signal_strength=10.0)
    cal = MaintenanceCalendar(tmp_path / "m.json")
    cal.add_window("2026-09-11T08:00:00+00:00", "2026-09-11T08:10:00+00:00", "Operational Maintenance", sensor_ids=["BFA8"])
    clf = dc.DowntimeClassifier([("operational", det.operational_detector)])
    det_res = dc.classify_downtime_event(
        "BFA8",
        "2026-09-11T08:02:00+00:00",
        "2026-09-11T08:08:00+00:00",
        context={"maintenance_calendar": cal},
        classifier=clf,
    )
    assert det_res.cause == "Operational Maintenance"
