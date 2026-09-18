# Reliability Metrics Reference — Uptime & Downtime (rule-based, no ML)

This document is the reference for the data-reliability KPIs implemented in
`scripts/utils/`. All logic is **rule-based — no machine learning is used**.
The metrics are derived directly from the ILDS reliability design discourse,
generalised for oil-pipeline sensor devices (BFA8 / BFA3 and similar).

## 1. Uptime metrics

Uptime is a measure of how often a sensor successfully transmits valid data
when it is expected to. `compute_uptime()` in
`scripts/utils/uptime_engine.py` produces, per sensor per window:

| Field | Meaning |
|---|---|
| `expected_data_points` | window seconds ÷ sensor cadence |
| `actual_data_points` | records received in the window |
| `valid_data_points` | records that pass data-quality bounds |
| `late_data_points` | records whose ingestion delay exceeds the consonance threshold |
| `timestamp_consonance_pct` | (actual − late) / actual × 100 |
| `differ_rate_pct` | actual / expected × 100 |
| `data_quality_uptime_pct` | valid / expected × 100 |
| `uptime_pct` | (consonant + in-range) / expected × 100  ← the holistic uptime |
| `last_seen` | most recent sensor timestamp |
| `heartbeat_status` | up / degraded / down |

### 1.1 Timestamp consonance (time alignment)
Delay between `timestamp` (sensor-side) and `ingestion_timestamp` (pipeline-side).
A point is **late** when `abs(ingestion − sensor) > RELIABILITY_MAX_DELAY_SECONDS`.
Detects network latency, sensor clock drift, processing delays.

### 1.2 Differ rate (data-ingestion frequency)
`actual / expected` over the window, where `expected = window_seconds ÷ cadence`.
`RELIABILITY_EXPECTED_FREQUENCY_SECONDS` is the global default; per-sensor
overrides via `RELIABILITY_SENSOR_CADENCES="BFA8:300,BFA3:300"`. Identifies
missed transmissions.

### 1.3 Heartbeat monitoring (ping-based)
If no data is received within `RELIABILITY_HEARTBEAT_SECONDS`, the sensor is
considered `down`; within half the interval, `degraded`. The reliability
reporter turns heartbeat gaps into downtime events for classification.

### 1.4 Moving-average uptime (rolling window)
`moving_average_uptime()` averages per-window `uptime_pct` over the horizon
(`RELIABILITY_UPTIME_ROLLING_HORIZON_HOURS`, default 24h). Smoothes short
spikes without masking real outages. Written to
`data/kpi_reports/uptime/moving_average_<H>h.json`.

### 1.5 Data-quality uptime
Only records within `RELIABILITY_QUALITY_BOUNDS` (e.g. `BFA8:0:100`) count
toward uptime. Corrupted/incomplete/out-of-range data does not inflate uptime.

### 1.6 Multi-sensor correlation (redundancy check)
`RELIABILITY_CORRELATION_GROUPS="BFA8,BFA3"` groups sensors monitoring the
same segment. The report lists `reporting` / `missing` members and a
`corroborated_pct`. If Sensor A reports but B does not, B is flagged missing.

### 1.7 Uptime with exclusions
`uptime_with_exclusions()` splits downtime into `planned` / `uncontrollable`
/ `true_downtime` buckets and excludes planned + uncontrollable from the
denominator, so sensors are not penalised for pigging, weather, or LTE
outages they cannot control.

## 2. Downtime root-cause classification

The classifier (`scripts/utils/downtime_classifier.py`) walks categories in
priority order and assigns the **first** cause for which a detector returns a
positive signal:

```
network → hardware → software → operational → environmental
       → cybersecurity → pipeline → human → unknown
```

Each detector is a callable `(sensor_id, start, end, context) -> Detection | None`
in `scripts/utils/detectors/detectors.py`. Detectors return `None` when they
have no signal or are disabled, so the pipeline runs **offline by default**.

### 2.1 Network
| Cause | Signal |
|---|---|
| Airtel LTE Outage | outage API (`AIRTEL_OUTAGE_API_ENABLED`) for the sensor's region |
| Connectivity Failure (Wi-Fi/LTE/VPN/DNS) | `context["connectivity"]` verdict or local DNS/ping (only if `run_local_checks`) |

### 2.2 Hardware
| Cause | Signal (from telemetry / last records) |
|---|---|
| Power Failure / Battery Depletion | `battery_percent` ≤ 1 / < `RELIABILITY_MIN_BATTERY_PERCENT` |
| SIM Card Issue | `signal_strength` < `RELIABILITY_MIN_SIGNAL_STRENGTH` |
| Sensor Malfunction | `voltage` at rail extremes (≤0 or ≥5 V) |
| Clock Drift | max sensor/ingestion skew > 10× the consonance threshold |
| Cable Cut / Physical Damage | no data and no telemetry (`no_data_at_all`) |

### 2.3 Software
| Cause | Signal |
|---|---|
| Schema Validation Failure | `context["schema_errors"]` > 0 |
| Firmware Bug | `context["firmware"]["known_bad"]` |
| EC2 Processing Failure | `context["ec2_health"]["ok"]` false |
| Misconfigured Thresholds | `context["threshold_misconfigured"]` |

### 2.4 Operational (planned)
Cross-references the maintenance calendar
(`data/maintenance/maintenance_windows.json`). Priority: Emergency Shutdown →
Pigging Operation → Valve Operation → Sensor Recalibration → Operational
Maintenance. These causes are in `RELIABILITY_PLANNED_CAUSES` and excluded
from true uptime.

### 2.5 Environmental
| Cause | Signal |
|---|---|
| Temperature Extremes | `device_temperature` > 60 or < −20 °C |
| Extreme Weather | `context["weather"]["severe"]` (weather API, disabled by default) |
| Earthquake | `context["earthquake"]` |

### 2.6 Cybersecurity
Signal-based from `context["security"]` (supplied by a CloudWatch/GuardDuty
sidecar): `unauthorized_access`, `ddos`, `rate_limited`, `credential_leak`.
No active security scanning is performed.

### 2.7 Data pipeline
| Cause | Signal (when `PIPELINE_HEALTH_ENABLED`) |
|---|---|
| S3 Upload Failure | `pipeline_health.s3_upload_failed` |
| SNS Notification Failure | `pipeline_health.sns_failed` |
| SQS Backlog | backlog > `PIPELINE_SQS_BACKLOG_THRESHOLD` |
| EC2 Processing Delay | S3 latency > `PIPELINE_S3_LATENCY_THRESHOLD_SECONDS` |

### 2.8 Human error
Signal-based from `context["human_error"]` (operators supply verdicts; the
detector never infers human error from data alone to avoid false blame):
`incorrect_placement`, `manual_override`, `mislabelled_data`,
`forgotten_power_off`.

## 3. Report shape

Each window produces a reliability report (`data/kpi_reports/uptime/*.uptime.json`):

```json
{
  "schema_version": 1,
  "window_start": "...", "window_end": "...",
  "uptime": { "sensors": { "<sensor_id>": { ...per-field... }, "correlation_groups": [...] } },
  "downtime_event_count": N,
  "downtime_events": [ { "sensor_id", "start", "end", "cause", "category", "confidence", "evidence" } ],
  "uptime_with_exclusions": { "<sensor_id>": { "planned_downtime_seconds", "true_downtime_seconds", "uptime_pct" } },
  "cause_breakdown": { "by_cause": {...}, "by_bucket": {...} }
}
```

Classified downtime events are also appended to
`data/downtime/downtime_events.jsonl` (JSONL) for the dashboard's timeline and
root-cause breakdown.

## 4. Edge cases handled

| Edge case | Handling |
|---|---|
| Sensor clock drift | consonance marks late; hardware detector flags drift > 10× threshold |
| Bursty transmission | per-window binning by cadence; differ rate uses counts, not spacing |
| Network latency spikes | moving-average uptime smooths short spikes |
| False-positive outages | detectors are independent and composable; cross-validate via multiple `context` signals |
| Overlapping maintenance + outage | classifier priority (network before operational) + exclusions keep both |
| Missing expected frequency | adaptive via per-sensor cadence overrides; global default fallback |
| No telemetry at all | `no_data_at_all` → Cable Cut / Physical Damage (low confidence) |

## 5. Configuration

All reliability tuning is in `ReliabilityConfig` (`config/aws_config.py`) and
documented in `.env.example`. Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `RELIABILITY_EXPECTED_FREQUENCY_SECONDS` | 300 | global sensor cadence |
| `RELIABILITY_MAX_DELAY_SECONDS` | 60 | consonance threshold |
| `RELIABILITY_HEARTBEAT_SECONDS` | 600 | heartbeat / gap threshold |
| `RELIABILITY_QUALITY_BOUNDS` | — | data-quality ranges |
| `RELIABILITY_CORRELATION_GROUPS` | — | redundancy groups |
| `RELIABILITY_PLANNED_CAUSES` | maintenance/pigging/valve/recalibration | excluded from true uptime |
| `RELIABILITY_UNCONTROLLABLE_CAUSES` | LTE/weather/DDoS/earthquake | excluded from true uptime |
| `AIRTEL_OUTAGE_API_ENABLED` | false | Airtel LTE detector (offline by default) |
| `WEATHER_API_ENABLED` | false | weather detector (offline by default) |
| `PIPELINE_HEALTH_ENABLED` | false | SQS/S3 pipeline health checks |
