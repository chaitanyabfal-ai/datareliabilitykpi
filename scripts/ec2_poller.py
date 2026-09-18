#!/usr/bin/env python3
"""
ec2_poller.py -- long-polls the SQS queue fed by the S3 -> SNS -> SQS fan-out,
unwraps the SNS envelope, fetches each raw sensor object from S3, computes
per-file KPI metrics, and writes idempotent window reports for the
dashboard and the kpi_aggregator.py rollups.

    AWS S3 (raw-sensor-data/) -> S3 event -> SNS -> SQS -> (this script)
        -> data/kpi_reports/windows/*.json
        -> data/kpi_reports/ec2_queue_kpi_latest.json

In addition to the base KPI windows (mean/min/max/std/rate-of-change), this
poller also computes the data-reliability KPIs (uptime/downtime) for the
time span covered by each ingested file, using the rule-based reliability
reporter (no ML):

        -> data/kpi_reports/uptime/*.uptime.json
        -> data/downtime/downtime_events.jsonl

Run continuously as a systemd service (see systemd/ec2-kpi-poller.service)
or by hand for testing.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from config.aws_config import (  # noqa: E402
    AWS, KPI, KPI_REPORTS_DIR, KPI_WINDOWS_DIR, LOG_DIR, REPO_ROOT,
)
from scripts.utils.downtime_classifier import build_default_classifier  # noqa: E402
from scripts.utils.downtime_event_log import DowntimeEventLog  # noqa: E402
from scripts.utils.kpi_engine import compute_kpis  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.reliability_reporter import report_reliability  # noqa: E402
from scripts.utils.schema import validate_bytes  # noqa: E402
from scripts.utils.state_store import StateStore  # noqa: E402
from scripts.utils.uptime_engine import parse_timestamp  # noqa: E402

logger = get_logger("ec2_poller", LOG_DIR / "ec2_poller.log")

LATEST_REPORT_PATH = KPI_REPORTS_DIR / "ec2_queue_kpi_latest.json"


def build_clients():
    session = boto3.Session(region_name=AWS.region)
    return session.client("sqs"), session.client("s3")


def _safe_window_filename(object_key: str) -> str:
    return object_key.replace("/", "__") + ".kpi.json"


def _reliability_window(records: list[dict]) -> tuple[datetime, datetime] | None:
    """Derive the [start, end] reliability window from the min/max sensor
    timestamps in a file's records. Falls back to now if not parseable."""
    timestamps = [parse_timestamp(r.get("timestamp")) for r in records]
    timestamps = [t for t in timestamps if t is not None]
    if not timestamps:
        return None
    start = min(timestamps)
    end = max(timestamps)
    if end == start:
        end = start.replace() if False else end
    # Ensure the window has at least the heartbeat length so gap detection works.
    return start, end


def process_object(s3_client, bucket: str, object_key: str, state: StateStore) -> bool:
    """
    Fetch, validate, and compute KPIs for one S3 object, then compute the
    reliability (uptime/downtime) KPIs for the file's time span. Writes a
    window report and returns True on success (including the "already
    processed" no-op case, so the caller can safely ack the SQS message).
    """
    dedupe_key = f"{bucket}/{object_key}"
    if state.has(dedupe_key):
        logger.debug("Object already processed, skipping: %s", dedupe_key)
        return True

    try:
        response = s3_client.get_object(Bucket=bucket, Key=object_key)
        raw = response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to fetch s3://%s/%s: %s", bucket, object_key, exc)
        return False

    records, validation = validate_bytes(raw, object_key)
    if not validation.ok:
        logger.warning("Skipping invalid object s3://%s/%s: %s", bucket, object_key, validation.errors[:5])
        state.set(dedupe_key, {"status": "invalid", "processed_at": time.time()})
        return True

    kpi_result = compute_kpis(records, thresholds=KPI.thresholds)
    processed_at = datetime.now(timezone.utc).isoformat()
    window_report = {
        "schema_version": 1,
        "source_bucket": bucket,
        "source_key": object_key,
        "processed_at": processed_at,
        **kpi_result.to_dict(),
    }

    window_path = KPI_WINDOWS_DIR / _safe_window_filename(object_key)
    window_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = window_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(window_report, f, indent=2)
    tmp_path.rename(window_path)

    # ---- Reliability KPIs (uptime/downtime) for this file's time span. ---- #
    reliability = None
    try:
        window = _reliability_window(records)
        if window is not None:
            reliability = report_reliability(
                records=records,
                window_start=window[0],
                window_end=window[1],
                classifier=_get_classifier(),
                event_log=_get_event_log(),
                write_reports=True,
            )
    except Exception as exc:  # reliability must never block the base KPI pipeline
        logger.warning("Reliability reporting failed for %s: %s", object_key, exc)

    with open(LATEST_REPORT_PATH, "w") as f:
        latest_payload = {
            "latest_key": object_key,
            "latest_bucket": bucket,
            "status": "processed",
            "timestamp": time.time(),
            "processed_at": processed_at,
            "total_records": kpi_result.total_records,
            "sensor_count": len(kpi_result.sensors),
        }
        if reliability is not None:
            latest_payload["reliability_window"] = {
                "window_start": reliability["window_start"],
                "window_end": reliability["window_end"],
                "downtime_event_count": reliability["downtime_event_count"],
            }
        json.dump(latest_payload, f, indent=2)

    state.set(dedupe_key, {"status": "processed", "processed_at": time.time(), "window_file": str(window_path.name)})
    logger.info(
        "Processed s3://%s/%s -> %s (%d records, %d sensors)",
        bucket, object_key, window_path.name, kpi_result.total_records, len(kpi_result.sensors),
    )
    return True


_CLASSIFIER = None
_EVENT_LOG: DowntimeEventLog | None = None


def _get_classifier():
    global _CLASSIFIER
    if _CLASSIFIER is None:
        _CLASSIFIER = build_default_classifier()
    return _CLASSIFIER


def _get_event_log() -> DowntimeEventLog:
    global _EVENT_LOG
    if _EVENT_LOG is None:
        _EVENT_LOG = DowntimeEventLog()
    return _EVENT_LOG


def _iter_s3_records_from_sqs_body(body: str):
    """An SQS message body is an SNS envelope; SNS's Message field is itself
    the raw S3 event notification JSON, which can contain multiple Records."""
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        logger.error("SQS message body is not valid JSON, skipping")
        return

    sns_message = envelope.get("Message")
    s3_event = json.loads(sns_message) if sns_message else envelope

    for record in s3_event.get("Records", []):
        try:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
        except KeyError:
            logger.warning("Skipping malformed S3 event record: %s", record)
            continue
        yield bucket, unquote_plus(key)


def poll_once(sqs_client, s3_client, state: StateStore) -> int:
    processed = 0
    try:
        response = sqs_client.receive_message(
            QueueUrl=AWS.sqs_queue_url,
            MaxNumberOfMessages=AWS.max_messages_per_poll,
            WaitTimeSeconds=AWS.wait_time_seconds,
            VisibilityTimeout=AWS.visibility_timeout,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("SQS receive_message failed: %s", exc)
        time.sleep(5)
        return 0

    messages = response.get("Messages", [])
    for msg in messages:
        all_ok = True
        for bucket, key in _iter_s3_records_from_sqs_body(msg["Body"]):
            all_ok = process_object(s3_client, bucket, key, state) and all_ok

        if all_ok:
            try:
                sqs_client.delete_message(QueueUrl=AWS.sqs_queue_url, ReceiptHandle=msg["ReceiptHandle"])
                processed += 1
            except (BotoCoreError, ClientError) as exc:
                logger.error("Failed to delete SQS message: %s", exc)
        else:
            logger.warning(
                "Leaving message %s in-flight for retry; it will be redelivered "
                "after the visibility timeout and eventually routed to the DLQ.",
                msg.get("MessageId"),
            )

    return processed


def main() -> None:
    if not AWS.sqs_queue_url:
        logger.error("SQS_QUEUE_URL is not set in .env -- nothing to poll")
        sys.exit(1)

    sqs_client, s3_client = build_clients()
    state = StateStore(REPO_ROOT / "data" / "state" / "ec2_poller_state.json")

    logger.info("Starting EC2 KPI poller on queue %s", AWS.sqs_queue_url)
    try:
        while True:
            n = poll_once(sqs_client, s3_client, state)
            if n:
                logger.info("Processed %d message(s) this cycle", n)
    except KeyboardInterrupt:
        logger.info("Shutting down on Ctrl-C")


if __name__ == "__main__":
    main()
