#!/usr/bin/env python3
"""
aws_s3_setup_check.py — preflight check that the AWS side of the pipeline
(S3 bucket, SNS topic, SQS queue + DLQ, S3 event notification, IAM
permissions) is actually wired up before you start the poller.

    python3 scripts/aws_s3_setup_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from config.aws_config import AWS, GARAGE  # noqa: E402


def check(label: str, fn) -> bool:
    try:
        fn()
        print(f"[OK]   {label}")
        return True
    except (BotoCoreError, ClientError) as exc:
        print(f"[FAIL] {label}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label}: {exc}")
        return False


def main() -> int:
    session = boto3.Session(region_name=AWS.region)
    s3 = session.client("s3")
    sns = session.client("sns")
    sqs = session.client("sqs")

    results = []

    results.append(check(
        f"AWS S3 bucket '{AWS.raw_bucket}' reachable",
        lambda: s3.head_bucket(Bucket=AWS.raw_bucket),
    ))

    def _check_notification():
        cfg = s3.get_bucket_notification_configuration(Bucket=AWS.raw_bucket)
        topics = cfg.get("TopicConfigurations", [])
        if not topics:
            raise RuntimeError("no TopicConfigurations found on bucket — S3 -> SNS event notification is missing")

    results.append(check("S3 -> SNS event notification configured", _check_notification))

    if AWS.sns_topic_arn:
        results.append(check(
            f"SNS topic '{AWS.sns_topic_arn}' reachable",
            lambda: sns.get_topic_attributes(TopicArn=AWS.sns_topic_arn),
        ))
    else:
        print("[SKIP] SNS_TOPIC_ARN not set in .env")

    if AWS.sqs_queue_url:
        results.append(check(
            f"SQS queue '{AWS.sqs_queue_url}' reachable",
            lambda: sqs.get_queue_attributes(QueueUrl=AWS.sqs_queue_url, AttributeNames=["All"]),
        ))

        def _check_dlq_redrive():
            attrs = sqs.get_queue_attributes(QueueUrl=AWS.sqs_queue_url, AttributeNames=["RedrivePolicy"])
            if "RedrivePolicy" not in attrs.get("Attributes", {}):
                raise RuntimeError("queue has no RedrivePolicy — messages that repeatedly fail will loop forever instead of landing in a DLQ")

        results.append(check("SQS queue has a dead-letter redrive policy", _check_dlq_redrive))
    else:
        print("[SKIP] SQS_QUEUE_URL not set in .env")

    print()
    if not GARAGE.is_configured():
        print("[WARN] Garage credentials not set — garage_uploader.py / garage_sync.py will not run.")

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
