#!/usr/bin/env python3
"""
garage_sync.py — polls Garage S3 (staged by garage_uploader.py) for new
objects, validates them, and uploads them to AWS S3 under raw-sensor-data/.

    Garage S3 (Tailscale) --(this script)--> AWS S3 raw-sensor-data/

Run continuously (`python3 garage_sync.py`) or as a one-shot pass suitable
for cron (`python3 garage_sync.py --once`).
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from config.aws_config import AWS, GARAGE, LOG_DIR, REPO_ROOT  # noqa: E402
from scripts.s3_uploader import upload_validated_object  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.state_store import StateStore  # noqa: E402

logger = get_logger("garage_sync", LOG_DIR / "garage_sync.log")

POLL_INTERVAL_SECONDS = 5


def build_garage_client():
    return boto3.client(
        "s3",
        endpoint_url=GARAGE.endpoint_url,
        aws_access_key_id=GARAGE.access_key_id,
        aws_secret_access_key=GARAGE.secret_access_key,
        region_name=GARAGE.region,
    )


def build_aws_client():
    return boto3.client("s3", region_name=AWS.region)


def list_garage_objects(garage_client) -> list[dict]:
    paginator = garage_client.get_paginator("list_objects_v2")
    objects: list[dict] = []
    try:
        for page in paginator.paginate(Bucket=GARAGE.bucket):
            objects.extend(page.get("Contents", []))
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to list Garage bucket %s: %s", GARAGE.bucket, exc)
    return objects


def sync_once(garage_client, aws_client, state: StateStore) -> int:
    objects = list_garage_objects(garage_client)
    synced = 0
    for obj in objects:
        key = obj["Key"]
        etag = obj.get("ETag", "")
        state_key = f"{key}:{etag}"
        if state.has(state_key):
            continue

        try:
            response = garage_client.get_object(Bucket=GARAGE.bucket, Key=key)
            body = response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            logger.error("Failed to download %s from Garage: %s", key, exc)
            continue

        now = datetime.now(timezone.utc)
        dest_key = f"{AWS.raw_prefix.rstrip('/')}/{now:%Y/%m/%d}/{key}"

        ok = upload_validated_object(aws_client, AWS.raw_bucket, dest_key, body, key)
        if ok:
            state.set(state_key, {"synced_at": now.isoformat(), "dest_key": dest_key})
            synced += 1
            logger.info("Synced %s -> s3://%s/%s", key, AWS.raw_bucket, dest_key)

    if synced:
        logger.info("Sync pass complete: %d object(s) moved to AWS S3", synced)
    return synced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit.")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS, help="Seconds between polls in continuous mode.")
    args = parser.parse_args()

    if not GARAGE.is_configured():
        logger.error("Garage credentials are not configured. Set GARAGE_ACCESS_KEY_ID / GARAGE_SECRET_ACCESS_KEY in .env")
        sys.exit(1)

    garage_client = build_garage_client()
    aws_client = build_aws_client()
    state = StateStore(REPO_ROOT / "data" / "state" / "garage_sync_state.json")

    if args.once:
        sync_once(garage_client, aws_client, state)
        return

    logger.info("Starting continuous Garage -> AWS S3 sync (interval=%.1fs)", args.interval)
    try:
        while True:
            sync_once(garage_client, aws_client, state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Shutting down on Ctrl-C")


if __name__ == "__main__":
    main()
