import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.garage_uploader as garage_uploader  # noqa: E402
from scripts.garage_uploader import _is_ignorable, upload_file  # noqa: E402
from scripts.utils.state_store import StateStore  # noqa: E402


def test_is_ignorable_skips_temp_and_dotfiles(tmp_path):
    tmp_file = tmp_path / "data.tmp"
    tmp_file.write_text("x")
    dotfile = tmp_path / ".hidden.json"
    dotfile.write_text("x")
    real_file = tmp_path / "data.json"
    real_file.write_text("x")

    assert _is_ignorable(tmp_file) is True
    assert _is_ignorable(dotfile) is True
    assert _is_ignorable(real_file) is False


def test_upload_file_is_idempotent(tmp_path):
    sensor_file = tmp_path / "reading.json"
    sensor_file.write_text('[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]')

    state = StateStore(tmp_path / "state.json")
    s3_client = MagicMock()

    ok1 = upload_file(s3_client, sensor_file, state)
    ok2 = upload_file(s3_client, sensor_file, state)

    assert ok1 is True
    assert ok2 is True
    s3_client.put_object.assert_called_once()  # second call was a no-op due to state store


def test_upload_file_retries_then_succeeds(tmp_path, monkeypatch):
    from botocore.exceptions import ClientError

    sensor_file = tmp_path / "reading2.json"
    sensor_file.write_text('[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]')

    state = StateStore(tmp_path / "state2.json")
    s3_client = MagicMock()
    error = ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
    s3_client.put_object.side_effect = [error, None]

    monkeypatch.setattr(time, "sleep", lambda *_: None)  # skip real backoff delay in tests

    ok = upload_file(s3_client, sensor_file, state)

    assert ok is True
    assert s3_client.put_object.call_count == 2


def test_upload_file_deletes_local_file_after_success(tmp_path, monkeypatch):
    sensor_file = tmp_path / "reading3.json"
    sensor_file.write_text('[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]')

    state = StateStore(tmp_path / "state3.json")
    s3_client = MagicMock()
    monkeypatch.setattr(
        garage_uploader,
        "SYNCTHING",
        replace(garage_uploader.SYNCTHING, delete_after_upload=True),
    )

    assert upload_file(s3_client, sensor_file, state) is True
    assert not sensor_file.exists()


def test_upload_file_deletes_state_recorded_file_when_cleanup_enabled(tmp_path, monkeypatch):
    sensor_file = tmp_path / "reading4.json"
    sensor_file.write_text('[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]')

    state = StateStore(tmp_path / "state4.json")
    s3_client = MagicMock()
    state.set(garage_uploader._state_key(sensor_file), {"uploaded_at": time.time()})
    monkeypatch.setattr(
        garage_uploader,
        "SYNCTHING",
        replace(garage_uploader.SYNCTHING, delete_after_upload=True),
    )

    assert upload_file(s3_client, sensor_file, state) is True
    assert not sensor_file.exists()
    s3_client.put_object.assert_not_called()


def test_upload_file_uploads_directly_to_aws_when_garage_is_disabled(tmp_path, monkeypatch):
    sensor_file = tmp_path / "reading5.json"
    sensor_file.write_text('[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]')

    state = StateStore(tmp_path / "state5.json")
    s3_client = MagicMock()
    monkeypatch.setattr(garage_uploader, "UPLOAD_TARGET", "aws")
    monkeypatch.setattr(garage_uploader, "GARAGE", MagicMock(is_configured=MagicMock(return_value=False)))
    monkeypatch.setattr(garage_uploader, "AWS", MagicMock(raw_bucket="raw-sensor-data-bucket", raw_prefix="raw-sensor-data/", region="us-east-1"))
    monkeypatch.setattr(garage_uploader, "SYNCTHING", replace(garage_uploader.SYNCTHING, delete_after_upload=True))
    tagged = {}

    body = b'[{"timestamp": "2026-01-01T00:00:00Z", "sensor_id": "s1", "value": 1}]'
    sensor_file.write_bytes(body)

    assert upload_file(s3_client, sensor_file, state) is True
    s3_client.put_object.assert_called_once_with(
        Bucket="raw-sensor-data-bucket",
        Key="raw-sensor-data/reading5.json",
        Body=body,
    )
    assert not sensor_file.exists()
