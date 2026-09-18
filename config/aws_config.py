"""
Centralized, environment-driven configuration for the whole pipeline.

Every script in scripts/ imports from here instead of reading os.environ
directly, so there is exactly one place that defines defaults and validates
required settings.

This module extends the original kpidataonprempcs3 configuration with the
data-reliability layer: uptime/downtime tuning, sensor cadence, planned
maintenance windows, and external cause-detector settings (Airtel LTE,
weather, SCADA, pipeline health). All reliability KPI logic is rule-based --
no machine learning is used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", REPO_ROOT / "data")) or REPO_ROOT / "data"
LOG_DIR = Path(os.getenv("LOG_DIR", DATA_DIR / "logs")) or DATA_DIR / "logs"
KPI_REPORTS_DIR = Path(os.getenv("KPI_REPORTS_DIR", DATA_DIR / "kpi_reports")) or DATA_DIR / "kpi_reports"
KPI_WINDOWS_DIR = KPI_REPORTS_DIR / "windows"
KPI_HOURLY_DIR = KPI_REPORTS_DIR / "hourly"
KPI_DAILY_DIR = KPI_REPORTS_DIR / "daily"
KPI_UPTIME_DIR = KPI_REPORTS_DIR / "uptime"
KPI_DOWNTIME_DIR = KPI_REPORTS_DIR / "downtime"
MAINTENANCE_DIR = Path(os.getenv("MAINTENANCE_DIR", DATA_DIR / "maintenance")) or DATA_DIR / "maintenance"
MAINTENANCE_LOG_PATH = MAINTENANCE_DIR / "maintenance_windows.json"
CAUSE_LOG_PATH = Path(os.getenv("CAUSE_LOG_PATH", DATA_DIR / "downtime" / "downtime_events.jsonl")) or DATA_DIR / "downtime" / "downtime_events.jsonl"

for _d in (
    DATA_DIR, LOG_DIR, KPI_REPORTS_DIR, KPI_WINDOWS_DIR, KPI_HOURLY_DIR,
    KPI_DAILY_DIR, KPI_UPTIME_DIR, KPI_DOWNTIME_DIR, MAINTENANCE_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    val = os.getenv(name)
    if not val:
        return default or []
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass(frozen=True)
class SyncthingConfig:
    watch_dir: str = os.getenv("SYNC_WATCH_DIR", str(REPO_ROOT / "data" / "incoming_csvs"))
    ignore_suffixes: tuple[str, ...] = field(
        default_factory=lambda: tuple(_env_list("SYNC_IGNORE_SUFFIXES", [".tmp", ".part", ".swp"]))
    )
    settle_seconds: float = _env_float("SYNC_SETTLE_SECONDS", 0.5)
    delete_after_upload: bool = _env_bool("DELETE_AFTER_UPLOAD", False)


UPLOAD_TARGET = os.getenv("UPLOAD_TARGET", "aws")


@dataclass(frozen=True)
class GarageConfig:
    endpoint_url: str = os.getenv("GARAGE_ENDPOINT_URL", "http://100.78.2.20:3900")
    access_key_id: str = os.getenv("GARAGE_ACCESS_KEY_ID", "")
    secret_access_key: str = os.getenv("GARAGE_SECRET_ACCESS_KEY", "")
    bucket: str = os.getenv("GARAGE_BUCKET", "sensor-data-staging")
    region: str = os.getenv("GARAGE_REGION", "garage")

    def is_configured(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)


@dataclass(frozen=True)
class AWSConfig:
    region: str = os.getenv("AWS_REGION", "ap-south-1")
    raw_bucket: str = os.getenv("AWS_RAW_BUCKET", "raw-sensor-data-bucket")
    raw_prefix: str = os.getenv("AWS_RAW_PREFIX", "raw-sensor-data/")
    sns_topic_arn: str = os.getenv("SNS_TOPIC_ARN", "")
    sqs_queue_url: str = os.getenv("SQS_QUEUE_URL", "")
    sqs_dlq_url: str = os.getenv("SQS_DLQ_URL", "")
    max_messages_per_poll: int = _env_int("SQS_MAX_MESSAGES", 10)
    wait_time_seconds: int = _env_int("SQS_WAIT_TIME_SECONDS", 20)
    visibility_timeout: int = _env_int("SQS_VISIBILITY_TIMEOUT", 60)


@dataclass(frozen=True)
class KPIConfig:
    thresholds: dict = field(default_factory=dict)
    hourly_align_minutes: int = _env_int("KPI_HOURLY_ALIGN_MINUTES", 60)

    def __post_init__(self):
        # Only populate from env when the field was not explicitly provided
        # (i.e. is still empty). This lets callers/tests override these
        # derived dicts via dataclass.replace() without __post_init__
        # clobbering them with an empty env parse.
        if not self.thresholds:
            raw = os.getenv("KPI_THRESHOLDS", "")
            parsed = {}
            for chunk in raw.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                parts = chunk.split(":")
                if len(parts) == 3:
                    field_name, lo, hi = parts
                    try:
                        parsed[field_name] = (float(lo), float(hi))
                    except ValueError:
                        continue
            object.__setattr__(self, "thresholds", parsed)


@dataclass(frozen=True)
class ReliabilityConfig:
    """Tuning for the uptime/downtime reliability KPI layer (rule-based, no ML)."""

    # Expected per-sensor cadence in seconds. Either a single global default
    # (e.g. "300") or a per-sensor map "BFA8:300,BFA3:300" parsed into a dict.
    expected_frequency_seconds: int = _env_int("RELIABILITY_EXPECTED_FREQUENCY_SECONDS", 300)

    # Per-sensor cadence overrides, sensor_id -> seconds.
    sensor_cadences: dict = field(default_factory=dict)

    # Maximum acceptable delay between sensor timestamp and ingestion timestamp
    # before a data point is marked "late" (timestamp consonance).
    max_acceptable_delay_seconds: int = _env_int("RELIABILITY_MAX_DELAY_SECONDS", 60)

    # Heartbeat: no data within this many seconds => sensor considered down.
    heartbeat_interval_seconds: int = _env_int("RELIABILITY_HEARTBEAT_SECONDS", 600)

    # Rolling-window uptime: window size and the larger averaging horizon.
    uptime_window_seconds: int = _env_int("RELIABILITY_UPTIME_WINDOW_SECONDS", 3600)
    uptime_rolling_horizon_hours: int = _env_int("RELIABILITY_UPTIME_ROLLING_HORIZON_HOURS", 24)

    # Data-quality bounds used to invalidate records that should not count
    # toward uptime (e.g. pressure outside 0-100 BAR for the ILDS sensors).
    quality_bounds: dict = field(default_factory=dict)

    # Multi-sensor correlation: sensor_ids that monitor the same pipeline
    # segment and should corroborate each other (comma-separated groups,
    # groups separated by ';'). e.g. "BFA8,BFA3;LINE2A,LINE2B".
    correlation_groups: list = field(default_factory=list)

    # Causes that count as "planned" downtime (excluded from true uptime).
    planned_causes: tuple = field(default_factory=lambda: tuple(
        _env_list(
            "RELIABILITY_PLANNED_CAUSES",
            ["Operational Maintenance", "Pigging Operation", "Valve Operation", "Sensor Recalibration"],
        )
    ))

    # Causes that count as "uncontrollable" downtime (excluded from true uptime,
    # but tracked separately for reporting).
    uncontrollable_causes: tuple = field(default_factory=lambda: tuple(
        _env_list(
            "RELIABILITY_UNCONTROLLABLE_CAUSES",
            ["Airtel LTE Outage", "Extreme Weather", "Temperature Extremes", "DDoS Attack", "Earthquake"],
        )
    ))

    # Thresholds for hardware telemetry that classify a sensor as degraded.
    min_battery_percent: float = _env_float("RELIABILITY_MIN_BATTERY_PERCENT", 20.0)
    min_signal_strength: float = _env_float("RELIABILITY_MIN_SIGNAL_STRENGTH", 10.0)

    # External detector toggles. Disabled detectors return "unknown" so the
    # pipeline keeps running offline / without credentials.
    airtel_outage_api_enabled: bool = _env_bool("AIRTEL_OUTAGE_API_ENABLED", False)
    airtel_outage_api_url: str = os.getenv("AIRTEL_OUTAGE_API_URL", "")
    airtel_outage_api_token: str = os.getenv("AIRTEL_OUTAGE_API_TOKEN", "")

    weather_api_enabled: bool = _env_bool("WEATHER_API_ENABLED", False)
    weather_api_url: str = os.getenv("WEATHER_API_URL", "https://api.openweathermap.org/data/2.5/weather")
    weather_api_key: str = os.getenv("WEATHER_API_KEY", "")

    scada_api_enabled: bool = _env_bool("SCADA_API_ENABLED", False)
    scada_api_url: str = os.getenv("SCADA_API_URL", "")

    pipeline_health_enabled: bool = _env_bool("PIPELINE_HEALTH_ENABLED", False)
    sqs_backlog_threshold: int = _env_int("PIPELINE_SQS_BACKLOG_THRESHOLD", 1000)
    s3_latency_threshold_seconds: float = _env_float("PIPELINE_S3_LATENCY_THRESHOLD_SECONDS", 30.0)

    # Sensor locations keyed by sensor_id -> {lat, lon, region} used by the
    # Airtel outage and weather detectors. Format: "BFA8:28.19,76.61,Rewari".
    sensor_locations: dict = field(default_factory=dict)

    def __post_init__(self):
        # Only populate each derived field from env when it was not explicitly
        # provided (i.e. is still empty). This lets callers/tests override
        # them via dataclass.replace() without __post_init__ clobbering them.
        if not self.sensor_cadences:
            cadences = {}
            for item in _env_list("RELIABILITY_SENSOR_CADENCES", []):
                if ":" in item:
                    sid, secs = item.split(":", 1)
                    try:
                        cadences[sid.strip()] = int(secs)
                    except ValueError:
                        continue
            object.__setattr__(self, "sensor_cadences", cadences)

        if not self.quality_bounds:
            bounds = {}
            for item in _env_list("RELIABILITY_QUALITY_BOUNDS", []):
                parts = item.split(":")
                if len(parts) == 3:
                    name, lo, hi = parts
                    try:
                        bounds[name.strip()] = (float(lo), float(hi))
                    except ValueError:
                        continue
            object.__setattr__(self, "quality_bounds", bounds)

        if not self.correlation_groups:
            groups = []
            for group_str in os.getenv("RELIABILITY_CORRELATION_GROUPS", "").split(";"):
                ids = [s.strip() for s in group_str.split(",") if s.strip()]
                if ids:
                    groups.append(ids)
            object.__setattr__(self, "correlation_groups", groups)

        if not self.sensor_locations:
            locations = {}
            for item in _env_list("RELIABILITY_SENSOR_LOCATIONS", []):
                parts = item.split(":")
                if len(parts) == 4:
                    sid, lat, lon, region = parts
                    try:
                        locations[sid.strip()] = {"lat": float(lat), "lon": float(lon), "region": region.strip()}
                    except ValueError:
                        continue
            object.__setattr__(self, "sensor_locations", locations)

    def cadence_for(self, sensor_id: str) -> int:
        return self.sensor_cadences.get(sensor_id, self.expected_frequency_seconds)


SYNCTHING = SyncthingConfig()
GARAGE = GarageConfig()
AWS = AWSConfig()
KPI = KPIConfig()
RELIABILITY = ReliabilityConfig()


def summarize() -> str:
    return (
        f"Syncthing watch_dir={SYNCTHING.watch_dir}\n"
        f"Garage endpoint={GARAGE.endpoint_url} bucket={GARAGE.bucket} "
        f"configured={GARAGE.is_configured()}\n"
        f"AWS region={AWS.region} raw_bucket={AWS.raw_bucket} "
        f"sqs_queue_url={'set' if AWS.sqs_queue_url else 'unset'} "
        f"sns_topic_arn={'set' if AWS.sns_topic_arn else 'unset'}\n"
        f"KPI thresholds={KPI.thresholds or '(none configured)'}\n"
        f"Reliability cadence={RELIABILITY.expected_frequency_seconds}s "
        f"max_delay={RELIABILITY.max_acceptable_delay_seconds}s "
        f"heartbeat={RELIABILITY.heartbeat_interval_seconds}s "
        f"uptime_window={RELIABILITY.uptime_window_seconds}s "
        f"correlation_groups={RELIABILITY.correlation_groups or '(none)'}"
    )


if __name__ == "__main__":
    print(summarize())
