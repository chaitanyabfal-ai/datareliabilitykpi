"""
Sensor payload schema validation, shared by s3_uploader.py, garage_sync.py,
and ec2_poller.py so every hop agrees on what a "valid" sensor record is.

Required shape (JSON list of records, or CSV with the same columns):

    [
        {"timestamp": "2026-09-11T08:00:00Z", "sensor_id": "line1-temp", "value": 72.4},
        ...
    ]

The reliability layer additionally consumes optional telemetry fields when
present -- they are NOT required by the base schema, only forwarded:

    ingestion_timestamp  -> used for timestamp-consonance uptime (delay vs. timestamp)
    battery_percent      -> hardware-cause detector (power/battery depletion)
    signal_strength      -> hardware-cause detector (SIM/LTE health)
    device_temperature   -> environmental-cause detector

Extra fields are always passed through untouched; only the required fields
below are enforced.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone

REQUIRED_FIELDS = ("timestamp", "sensor_id", "value")

OPTIONAL_TELEMETRY_FIELDS = (
    "ingestion_timestamp",
    "battery_percent",
    "signal_strength",
    "device_temperature",
)


@dataclass
class ValidationResult:
    ok: bool
    record_count: int
    errors: list[str]


def _validate_records(records: list[dict]) -> ValidationResult:
    errors: list[str] = []
    if not isinstance(records, list):
        return ValidationResult(ok=False, record_count=0, errors=["payload is not a list of records"])

    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append(f"record[{i}] is not an object")
            continue
        for field_name in REQUIRED_FIELDS:
            if field_name not in rec:
                errors.append(f"record[{i}] missing required field '{field_name}'")
        if "value" in rec:
            try:
                float(rec["value"])
            except (TypeError, ValueError):
                errors.append(f"record[{i}] field 'value' is not numeric: {rec.get('value')!r}")

    return ValidationResult(ok=not errors, record_count=len(records), errors=errors)


def validate_json_bytes(raw: bytes) -> tuple[list[dict], ValidationResult]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [], ValidationResult(ok=False, record_count=0, errors=[f"invalid JSON: {exc}"])
    if isinstance(parsed, dict):
        parsed = [parsed]
    return parsed, _validate_records(parsed)


def validate_csv_bytes(raw: bytes) -> tuple[list[dict], ValidationResult]:
    try:
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        records = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        return [], ValidationResult(ok=False, record_count=0, errors=[f"invalid CSV: {exc}"])
    return records, _validate_records(records)


def validate_bytes(raw: bytes, filename: str) -> tuple[list[dict], ValidationResult]:
    if filename.lower().endswith(".csv"):
        try:
            text = raw.decode("utf-8")
            source_records = list(csv.DictReader(io.StringIO(text)))
        except (UnicodeDecodeError, csv.Error) as exc:
            return [], ValidationResult(ok=False, record_count=0, errors=[f"invalid CSV: {exc}"])

        field_names = {key.lower() for key in (source_records[0] if source_records else {})}
        if {"timestamp", "voltage"}.issubset(field_names) and not {"sensor_id", "value"}.issubset(field_names):
            sensor_id = filename.split("_", 1)[0]
            records = []
            for row in source_records:
                raw_timestamp = row.get("Timestamp", row.get("timestamp", ""))
                raw_value = row.get("Voltage", row.get("voltage", ""))
                try:
                    timestamp_value = float(raw_timestamp)
                    if timestamp_value > 10_000_000_000:
                        timestamp_value /= 1000
                    timestamp = datetime.fromtimestamp(timestamp_value, tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    timestamp = raw_timestamp
                rec = {"timestamp": timestamp, "sensor_id": sensor_id, "value": raw_value}
                for telemetry_field in OPTIONAL_TELEMETRY_FIELDS:
                    if telemetry_field in row:
                        rec[telemetry_field] = row[telemetry_field]
                records.append(rec)
            return records, _validate_records(records)

        return source_records, _validate_records(source_records)
    return validate_json_bytes(raw)
