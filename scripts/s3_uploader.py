#!/usr/bin/env python3
"""
s3_uploader.py — validates sensor payloads against the expected schema and
uploads them to AWS S3 with the correct prefix.

Used as a library by garage_sync.py; also runnable standalone for manual
uploads / debugging:

    python3 scripts/s3_uploader.py path/to/file.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from config.aws_config import AWS, LOG_DIR  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.schema import validate_bytes  # noqa: E402

logger = get_logger("s3_uploader", LOG_DIR / "s3_uploader.log")


def upload_validated_object(s3_client, bucket: str, dest_key: str, body: bytes, source_name: str) -> bool:
    """
    Validate `body` (bytes of a JSON or CSV sensor payload) against the
    shared schema, and if valid, PUT it to `bucket`/`dest_key`.

    Returns True on a successful, valid upload; False otherwise (invalid
    payloads are never uploaded, so downstream KPI processing never has to
    special-case malformed data).
    """
    records, result = validate_bytes(body, source_name)
    if not result.ok:
        logger.warning(
            "Rejecting %s: schema validation failed (%d record(s), errors=%s)",
            source_name, result.record_count, result.errors[:5],
        )
        return False

    try:
        s3_client.put_object(Bucket=bucket, Key=dest_key, Body=body)
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to upload %s to s3://%s/%s: %s", source_name, bucket, dest_key, exc)
        return False

    logger.info(
        "Validated + uploaded %s -> s3://%s/%s (%d record(s))",
        source_name, bucket, dest_key, result.record_count,
    )
    return True


def main() -> None:
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Local sensor file (.json or .csv) to validate and upload.")
    parser.add_argument("--bucket", default=AWS.raw_bucket)
    parser.add_argument("--prefix", default=AWS.raw_prefix)
    args = parser.parse_args()

    path = Path(args.file)
    body = path.read_bytes()
    dest_key = f"{args.prefix.rstrip('/')}/{path.name}"

    client = boto3.client("s3", region_name=AWS.region)
    ok = upload_validated_object(client, args.bucket, dest_key, body, path.name)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
