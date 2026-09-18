#!/usr/bin/env python3
"""
garage_uploader.py — watches the local Syncthing "Receive Only" folder and
uploads completed sensor files to Garage S3 over Tailscale.

This is the first hop of the pipeline requested in the new architecture:

    On-Prem Server --(Syncthing)--> PC folder --(this script)--> Garage S3

Design notes
------------
- Uses `watchdog` for near-real-time filesystem events (paired with
  Syncthing's "Watch for Changes" setting -- see SYNCTHING_SETUP.md).
- Ignores partial/temp files (`.tmp`, `.part`, dotfiles) so it never uploads
  a half-written sensor file, even if Syncthing's own atomic-write handling
  is bypassed by some other writer.
- Debounces: waits `settle_seconds` after a create/modify event before
  reading the file, to dodge the tiny race where the event fires a moment
  before the OS finishes flushing to disk.
- Idempotent: a JSON state store keyed by (filename, size, mtime) prevents
  duplicate uploads across restarts and Syncthing rescans.
- Retries failed uploads with exponential backoff instead of dropping them.
- Also supports a `--backfill` pass over files already sitting in the
  watch directory before it starts watching for new ones (useful after a
  restart or when first pointing this at an already-populated folder).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402
from botocore.config import Config as BotoConfig  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from watchdog.events import FileSystemEventHandler  # noqa: E402
from watchdog.observers import Observer  # noqa: E402

from config.aws_config import AWS, GARAGE, SYNCTHING, LOG_DIR, UPLOAD_TARGET  # noqa: E402
from scripts.utils.logging_config import get_logger  # noqa: E402
from scripts.utils.schema import validate_bytes  # noqa: E402
from scripts.utils.state_store import StateStore  # noqa: E402

logger = get_logger("garage_uploader", LOG_DIR / "garage_uploader.log")

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5


def build_s3_client():
    if UPLOAD_TARGET == "aws":
        return boto3.client(
            "s3",
            region_name=AWS.region,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )

    if not GARAGE.is_configured():
        logger.warning(
            "GARAGE_ACCESS_KEY_ID / GARAGE_SECRET_ACCESS_KEY are not set — "
            "uploads will fail until they are configured in .env"
        )
    return boto3.client(
        "s3",
        endpoint_url=GARAGE.endpoint_url,
        aws_access_key_id=GARAGE.access_key_id,
        aws_secret_access_key=GARAGE.secret_access_key,
        region_name=GARAGE.region,
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _state_key(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"


def _is_ignorable(path: Path) -> bool:
    if path.is_dir():
        return True
    if path.name.startswith("."):
        return True
    if any(path.name.endswith(suffix) for suffix in SYNCTHING.ignore_suffixes):
        return True
    return False


def upload_file(s3_client, path: Path, state: StateStore) -> bool:
    """Upload one file to the configured target (AWS or Garage) with retry/backoff."""
    key = _state_key(path)
    if state.has(key):
        logger.debug("Skipping already-uploaded file: %s", path.name)
        if SYNCTHING.delete_after_upload:
            try:
                os.remove(path)
                logger.info("Deleted local file already staged: %s", path.name)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not delete %s after upload: %s", path.name, exc)
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(path, "rb") as f:
                body = f.read()
            checksum = hashlib.sha256(body).hexdigest()[:16]

            if UPLOAD_TARGET == "aws":
                records, result = validate_bytes(body, path.name)
                if not result.ok:
                    logger.warning(
                        "Rejecting %s: schema validation failed (%d record(s), errors=%s)",
                        path.name, result.record_count, result.errors[:5],
                    )
                    return False
                dest_key = f"{AWS.raw_prefix.rstrip('/')}/{path.name}"
                s3_client.put_object(Bucket=AWS.raw_bucket, Key=dest_key, Body=body)
            else:
                s3_client.put_object(
                    Bucket=GARAGE.bucket,
                    Key=path.name,
                    Body=body,
                    Metadata={"source-checksum": checksum},
                )

            state.set(key, {"uploaded_at": time.time(), "checksum": checksum})
            target_name = "AWS S3" if UPLOAD_TARGET == "aws" else "Garage S3"
            target_bucket = AWS.raw_bucket if UPLOAD_TARGET == "aws" else GARAGE.bucket
            logger.info("Uploaded %s to %s (bucket=%s, %d bytes)", path.name, target_name, target_bucket, len(body))

            if SYNCTHING.delete_after_upload:
                try:
                    os.remove(path)
                    logger.info("Deleted local file after staging: %s", path.name)
                except OSError as exc:
                    logger.warning("Could not delete %s after upload: %s", path.name, exc)
            return True

        except FileNotFoundError:
            logger.warning("File disappeared before upload (likely a transient temp file): %s", path)
            return False
        except (BotoCoreError, ClientError) as exc:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.error(
                "Upload attempt %d/%d failed for %s: %s (retrying in %.1fs)",
                attempt, MAX_RETRIES, path.name, exc, wait,
            )
            time.sleep(wait)

    logger.error("Giving up on %s after %d attempts", path.name, MAX_RETRIES)
    return False


class SyncthingHandler(FileSystemEventHandler):
    def __init__(self, s3_client, state: StateStore):
        self.s3_client = s3_client
        self.state = state

    def _handle(self, src_path: str) -> None:
        path = Path(src_path)
        if _is_ignorable(path):
            return
        # Debounce: give Syncthing's rename-into-place a moment to finish.
        time.sleep(SYNCTHING.settle_seconds)
        if not path.exists():
            return
        upload_file(self.s3_client, path, self.state)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        # Atomic rename (tmp -> final) fires as a "moved" event on most platforms.
        if not event.is_directory:
            self._handle(event.dest_path)


def backfill(s3_client, state: StateStore, watch_dir: Path) -> None:
    files = sorted(p for p in watch_dir.iterdir() if not _is_ignorable(p))
    logger.info("Backfilling %d existing file(s) from %s", len(files), watch_dir)
    for path in files:
        upload_file(s3_client, path, state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="Upload files already in the watch dir, then continue watching.")
    parser.add_argument("--once", action="store_true", help="Backfill existing files and exit (no watching).")
    args = parser.parse_args()

    watch_dir = Path(SYNCTHING.watch_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    s3_client = build_s3_client()
    state = StateStore(LOG_DIR.parent / "state" / "garage_uploader_state.json")

    if args.backfill or args.once:
        backfill(s3_client, state, watch_dir)
        if args.once:
            return

    handler = SyncthingHandler(s3_client, state)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    logger.info("Watching %s for new sensor files -> Garage S3 bucket %s", watch_dir, GARAGE.bucket)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down on Ctrl-C")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
