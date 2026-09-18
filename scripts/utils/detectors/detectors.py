"""
Cause-detector plugins for the downtime classifier.

Each detector is a callable (sensor_id, start, end, context) -> Detection | None.
Detectors return ``None`` when they have no positive signal (so the classifier
moves on) or when they are not configured (so the pipeline runs offline).

External detectors (Airtel LTE outage API, weather API, SCADA, AWS pipeline
health) are guarded behind the toggles in ReliabilityConfig: when the
corresponding ``*_enabled`` flag is False they return None and never touch the
network. All logic here is rule-based -- no machine learning.
"""
from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime

from config.aws_config import RELIABILITY
from scripts.utils.downtime_classifier import Detection
from scripts.utils.maintenance_calendar import get_default_calendar
from scripts.utils.uptime_engine import parse_timestamp, _coerce_float


def _telemetry(context: dict, sensor_id: str) -> dict:
    """Latest telemetry snapshot for a sensor, if the caller supplied one.

    Expected shape: context["telemetry"][sensor_id] = {"battery_percent": ...,
    "signal_strength": ..., "device_temperature": ..., "voltage": ...}.
    """
    return (context.get("telemetry") or {}).get(sensor_id, {})


def _last_records(context: dict, sensor_id: str) -> list[dict]:
    return (context.get("last_records") or {}).get(sensor_id, [])


# --------------------------------------------------------------------------- #
# 1. Network
# --------------------------------------------------------------------------- #
def airtel_outage_detector(sensor_id, start, end, context) -> Detection | None:
    """Airtel LTE outage: query the configured outage API for the sensor's region.

    Disabled (returns None) unless AIRTEL_OUTAGE_API_ENABLED=true. When
    enabled, calls the API (imported lazily so tests don't need the network)
    and classifies as "Airtel LTE Outage" if an outage overlaps the window.
    """
    if not RELIABILITY.airtel_outage_api_enabled:
        return None
    location = RELIABILITY.sensor_locations.get(sensor_id, {})
    region = location.get("region") or context.get("region") or "default"
    outages = _query_airtel_outage(region, start, end)
    if outages:
        return Detection(
            cause="Airtel LTE Outage",
            category="network",
            evidence={"region": region, "outages": outages},
        )
    return None


def connectivity_detector(sensor_id, start, end, context) -> Detection | None:
    """Wi-Fi/LTE/VPN/DNS connectivity failure.

    Two signal sources, both offline-friendly:
      - context["connectivity"] may carry a precomputed verdict
        (e.g. from a sidecar ping test): {"ok": False, "reason": "..."}.
      - Otherwise, if context["run_local_checks"] is set, a best-effort DNS
        lookup of the S3 endpoint and a ping to 8.8.8.8 are attempted; a
        failure => connectivity failure.
    """
    verdict = context.get("connectivity")
    if isinstance(verdict, dict) and verdict.get("ok") is False:
        return Detection(
            cause=f"Connectivity Failure ({verdict.get('reason', 'Wi-Fi/LTE/VPN')})",
            category="network",
            evidence=verdict,
        )
    if context.get("run_local_checks"):
        if not _dns_lookup("s3.amazonaws.com"):
            return Detection(
                cause="Connectivity Failure (DNS/firewall)",
                category="network",
                evidence={"dns_target": "s3.amazonaws.com"},
            )
        if not _ping("8.8.8.8"):
            return Detection(
                cause="Connectivity Failure (LTE/Wi-Fi)",
                category="network",
                evidence={"ping_target": "8.8.8.8"},
            )
    return None


# --------------------------------------------------------------------------- #
# 2. Hardware
# --------------------------------------------------------------------------- #
def hardware_detector(sensor_id, start, end, context) -> Detection | None:
    """Sensor malfunction / power failure / battery depletion / SIM / cable / clock drift.

    Uses the latest telemetry snapshot (if provided) and the last few records:
      - battery_percent < min          => Power Failure / Battery Depletion
      - signal_strength < min          => SIM Card Issue
      - voltage at rail extremes (0/5V) => Sensor Malfunction
      - clock skew between BFA8/BFA3    => Clock Drift (pair matching failure)
    """
    tel = _telemetry(context, sensor_id)
    battery = _coerce_float(tel.get("battery_percent"))
    signal = _coerce_float(tel.get("signal_strength"))
    voltage = _coerce_float(tel.get("voltage"))

    if battery is not None and battery < RELIABILITY.min_battery_percent:
        return Detection(
            cause="Power Failure" if battery <= 1 else "Battery Depletion",
            category="hardware",
            confidence=0.9,
            evidence={"battery_percent": battery},
        )
    if signal is not None and signal < RELIABILITY.min_signal_strength:
        return Detection(
            cause="SIM Card Issue",
            category="hardware",
            confidence=0.85,
            evidence={"signal_strength": signal},
        )
    if voltage is not None and (voltage <= 0.0 or voltage >= 5.0):
        return Detection(
            cause="Sensor Malfunction",
            category="hardware",
            evidence={"voltage": voltage},
        )

    # Clock drift: compare latest sensor timestamp vs ingestion time of last records.
    recs = _last_records(context, sensor_id)
    if recs:
        max_drift = 0.0
        for rec in recs:
            ts = parse_timestamp(rec.get("timestamp"))
            ing = parse_timestamp(rec.get("ingestion_timestamp")) or ts
            if ts and ing:
                max_drift = max(max_drift, abs((ing - ts).total_seconds()))
        if max_drift > RELIABILITY.max_acceptable_delay_seconds * 10:
            return Detection(
                cause="Clock Drift",
                category="hardware",
                confidence=0.8,
                evidence={"max_drift_seconds": max_drift},
            )
    # No telemetry and no records: a total signal loss hints at cable/physical damage.
    if context.get("no_data_at_all"):
        return Detection(
            cause="Cable Cut / Physical Damage",
            category="hardware",
            confidence=0.5,
            evidence={"reason": "no data and no telemetry received"},
        )
    return None


# --------------------------------------------------------------------------- #
# 3. Software
# --------------------------------------------------------------------------- #
def software_detector(sensor_id, start, end, context) -> Detection | None:
    """Firmware bug / EC2 processing failure / schema validation / misconfigured thresholds.

    Signals:
      - context["schema_errors"] > 0      => Schema Validation Failure
      - context["firmware"] carries a known bad version => Firmware Bug
      - context["ec2_health"] unhealthy   => EC2 Processing Failure
      - context["threshold_misconfigured"] => Misconfigured Thresholds
    """
    if context.get("schema_errors"):
        return Detection(
            cause="Schema Validation Failure",
            category="software",
            evidence={"schema_errors": context["schema_errors"]},
        )
    fw = context.get("firmware")
    if isinstance(fw, dict) and fw.get("known_bad"):
        return Detection(
            cause="Firmware Bug",
            category="software",
            evidence={"firmware_version": fw.get("version")},
        )
    ec2 = context.get("ec2_health")
    if isinstance(ec2, dict) and not ec2.get("ok", True):
        return Detection(
            cause="EC2 Processing Failure",
            category="software",
            evidence=ec2,
        )
    if context.get("threshold_misconfigured"):
        return Detection(
            cause="Misconfigured Thresholds",
            category="software",
            evidence={"detail": context.get("threshold_misconfigured")},
        )
    return None


# --------------------------------------------------------------------------- #
# 4. Operational (planned maintenance)
# --------------------------------------------------------------------------- #
def operational_detector(sensor_id, start, end, context) -> Detection | None:
    """Cross-reference the maintenance calendar for planned operational downtime.

    Matches (by priority) emergency shutdown, pigging, valve operation,
    sensor recalibration, or generic operational maintenance. The cause label
    is taken from the matching maintenance window.
    """
    calendar = context.get("maintenance_calendar") or get_default_calendar()
    hits = calendar.overlapping(start, end, sensor_id=sensor_id) if hasattr(calendar, "overlapping") else []
    if not hits:
        return None
    priority = [
        "Emergency Shutdown",
        "Pigging Operation",
        "Valve Operation",
        "Sensor Recalibration",
        "Operational Maintenance",
    ]
    chosen = None
    for cause in priority:
        for h in hits:
            if h.get("cause") == cause:
                chosen = h
                break
        if chosen:
            break
    if chosen is None:
        chosen = hits[0]
    return Detection(
        cause=chosen.get("cause", "Operational Maintenance"),
        category="operational",
        evidence={"maintenance_window_id": chosen.get("id"), "note": chosen.get("note", "")},
    )


# --------------------------------------------------------------------------- #
# 5. Environmental
# --------------------------------------------------------------------------- #
def environmental_detector(sensor_id, start, end, context) -> Detection | None:
    """Extreme weather / temperature extremes / dust / earthquake.

    Two offline-friendly signals:
      - device_temperature telemetry outside operating range.
      - weather API verdict supplied in context["weather"] (queried by the
        pipeline if WEATHER_API_ENABLED) reporting severe conditions.
    """
    tel = _telemetry(context, sensor_id)
    dev_temp = _coerce_float(tel.get("device_temperature"))
    if dev_temp is not None:
        if dev_temp > 60.0 or dev_temp < -20.0:
            return Detection(
                cause="Temperature Extremes",
                category="environmental",
                confidence=0.8,
                evidence={"device_temperature": dev_temp},
            )
    weather = context.get("weather")
    if isinstance(weather, dict) and weather.get("severe"):
        return Detection(
            cause="Extreme Weather",
            category="environmental",
            evidence=weather,
        )
    if context.get("earthquake"):
        return Detection(
            cause="Earthquake",
            category="environmental",
            evidence=context.get("earthquake"),
        )
    return None


# --------------------------------------------------------------------------- #
# 6. Cybersecurity
# --------------------------------------------------------------------------- #
def cybersecurity_detector(sensor_id, start, end, context) -> Detection | None:
    """Unauthorized access / DDoS / API rate limits / credential leaks.

    Pure signal-based: the pipeline (or a CloudWatch/GuardDuty sidecar) supplies
    verdicts in context["security"]; we never perform active security scans.
    """
    sec = context.get("security")
    if not isinstance(sec, dict):
        return None
    if sec.get("unauthorized_access"):
        return Detection(cause="Unauthorized AWS Access", category="cybersecurity", evidence=sec)
    if sec.get("ddos"):
        return Detection(cause="DDoS Attack", category="cybersecurity", evidence=sec)
    if sec.get("rate_limited"):
        return Detection(cause="API Rate Limit", category="cybersecurity", evidence=sec)
    if sec.get("credential_leak"):
        return Detection(cause="Credential Leak", category="cybersecurity", evidence=sec)
    return None


# --------------------------------------------------------------------------- #
# 7. Data pipeline
# --------------------------------------------------------------------------- #
def pipeline_detector(sensor_id, start, end, context) -> Detection | None:
    """S3 upload / SNS / SQS backlog / EC2 processing delay failures.

    When PIPELINE_HEALTH_ENABLED, the pipeline can fetch SQS backlog depth and
    S3 latency and pass them in context["pipeline_health"]. Otherwise the
    detector inspects the AWS resources lazily.
    """
    health = context.get("pipeline_health")
    if health is None and RELIABILITY.pipeline_health_enabled:
        health = _inspect_pipeline_health()
    if not isinstance(health, dict):
        return None
    if health.get("s3_upload_failed"):
        return Detection(cause="S3 Upload Failure", category="pipeline", evidence=health)
    if health.get("sns_failed"):
        return Detection(cause="SNS Notification Failure", category="pipeline", evidence=health)
    backlog = health.get("sqs_backlog") or 0
    if isinstance(backlog, (int, float)) and backlog > RELIABILITY.sqs_backlog_threshold:
        return Detection(cause="SQS Backlog", category="pipeline", evidence={"sqs_backlog": backlog})
    s3_latency = health.get("s3_latency_seconds")
    if isinstance(s3_latency, (int, float)) and s3_latency > RELIABILITY.s3_latency_threshold_seconds:
        return Detection(cause="EC2 Processing Delay", category="pipeline", evidence={"s3_latency_seconds": s3_latency})
    return None


# --------------------------------------------------------------------------- #
# 8. Human error
# --------------------------------------------------------------------------- #
def human_error_detector(sensor_id, start, end, context) -> Detection | None:
    """Incorrect placement / manual override / mislabelled data / forgotten power-off.

    Signal-based (operators supply verdicts via context["human_error"]); the
    detector does not infer human error from data alone to avoid false blame.
    """
    he = context.get("human_error")
    if not isinstance(he, dict):
        return None
    if he.get("incorrect_placement"):
        return Detection(cause="Incorrect Sensor Placement", category="human", evidence=he)
    if he.get("manual_override"):
        return Detection(cause="Manual Override", category="human", evidence=he)
    if he.get("mislabelled_data"):
        return Detection(cause="Mislabelled Data", category="human", evidence=he)
    if he.get("forgotten_power_off"):
        return Detection(cause="Forgotten Power-Off", category="human", evidence=he)
    return None


# --------------------------------------------------------------------------- #
# Offline-safe helper wrappers (network access only when explicitly enabled)
# --------------------------------------------------------------------------- #
def _query_airtel_outage(region: str, start: datetime, end: datetime) -> list[dict]:
    import requests  # imported lazily; only when the detector is enabled

    url = RELIABILITY.airtel_outage_api_url
    if not url:
        return []
    try:
        headers = {}
        if RELIABILITY.airtel_outage_api_token:
            headers["Authorization"] = f"Bearer {RELIABILITY.airtel_outage_api_token}"
        resp = requests.get(
            url,
            params={"region": region, "start": start.isoformat(), "end": end.isoformat()},
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    if isinstance(data, dict):
        return data.get("outages", []) if data.get("outages") else ([data] if data.get("active") else [])
    if isinstance(data, list):
        return data
    return []


def _dns_lookup(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _ping(target: str) -> bool:
    """Best-effort single ICMP ping; returns False if unreachable/unavailable.

    Only invoked when context["run_local_checks"] is set by the caller, so it
    never runs during normal offline operation or unit tests.
    """
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", "1000", target]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", target]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _inspect_pipeline_health() -> dict:
    """Fetch SQS backlog depth when pipeline health checks are enabled."""
    health: dict = {}
    try:
        import boto3  # imported lazily

        from config.aws_config import AWS

        if AWS.sqs_queue_url:
            sqs = boto3.client("sqs", region_name=AWS.region)
            attrs = sqs.get_queue_attributes(
                QueueUrl=AWS.sqs_queue_url,
                AttributeNames=["ApproximateNumberOfMessagesVisible"],
            )
            health["sqs_backlog"] = int(
                attrs.get("Attributes", {}).get("ApproximateNumberOfMessagesVisible", 0)
            )
    except Exception:
        pass
    return health
