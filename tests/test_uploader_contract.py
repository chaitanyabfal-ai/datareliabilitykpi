"""
Contract test: s3_uploader.upload_validated_object must reject invalid
payloads (never calling put_object) and accept valid ones.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.s3_uploader import upload_validated_object  # noqa: E402
from scripts.utils.schema import validate_bytes  # noqa: E402


def test_valid_payload_is_uploaded():
    client = MagicMock()
    body = json.dumps([{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1.0}]).encode()

    ok = upload_validated_object(client, "bucket", "raw-sensor-data/x.json", body, "x.json")

    assert ok is True
    client.put_object.assert_called_once()
    _, kwargs = client.put_object.call_args
    assert kwargs["Bucket"] == "bucket"
    assert kwargs["Key"] == "raw-sensor-data/x.json"


def test_missing_required_field_is_rejected():
    client = MagicMock()
    body = json.dumps([{"timestamp": "2026-01-01T00:00:00Z", "value": 1.0}]).encode()  # missing sensor_id

    ok = upload_validated_object(client, "bucket", "raw-sensor-data/bad.json", body, "bad.json")

    assert ok is False
    client.put_object.assert_not_called()


def test_non_numeric_value_is_rejected():
    client = MagicMock()
    body = json.dumps([{"timestamp": "t", "sensor_id": "s1", "value": "not-a-number"}]).encode()

    ok = upload_validated_object(client, "bucket", "raw-sensor-data/bad2.json", body, "bad2.json")

    assert ok is False
    client.put_object.assert_not_called()


def test_invalid_json_is_rejected():
    client = MagicMock()
    body = b"{not json"

    ok = upload_validated_object(client, "bucket", "raw-sensor-data/bad3.json", body, "bad3.json")

    assert ok is False
    client.put_object.assert_not_called()


def test_sensor_voltage_csv_is_normalized():
    body = b"Timestamp,Voltage\n1777461878.455297,-0.111\n"

    records, result = validate_bytes(body, "BFA5_Batch71084_2026-09-15.csv")

    assert result.ok is True
    assert records[0]["sensor_id"] == "BFA5"
    assert records[0]["value"] == "-0.111"
    assert records[0]["timestamp"].startswith("2026-")
