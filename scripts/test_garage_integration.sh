#!/usr/bin/env bash
# Verifies Tailscale connectivity to Garage and that the configured
# credentials can list/put/get against the staging bucket.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

GARAGE_ENDPOINT_URL="${GARAGE_ENDPOINT_URL:-http://100.78.2.20:3900}"
GARAGE_BUCKET="${GARAGE_BUCKET:-sensor-data-staging}"

echo "== 1. Tailscale reachability =="
HOST="$(echo "$GARAGE_ENDPOINT_URL" | sed -E 's#https?://([^:/]+).*#\1#')"
if command -v tailscale >/dev/null 2>&1; then
  tailscale status | grep -q "$HOST" && echo "OK: $HOST present in tailscale status" || echo "WARN: $HOST not found in tailscale status (may still be reachable)"
else
  echo "WARN: tailscale CLI not found on PATH, skipping status check"
fi

echo
echo "== 2. TCP reachability to Garage endpoint =="
if command -v curl >/dev/null 2>&1; then
  if curl -sS --max-time 5 -o /dev/null -w "HTTP %{http_code}\n" "$GARAGE_ENDPOINT_URL"; then
    echo "OK: Garage endpoint responded"
  else
    echo "FAIL: could not reach $GARAGE_ENDPOINT_URL"
    exit 1
  fi
else
  echo "WARN: curl not found, skipping"
fi

echo
echo "== 3. boto3 credential + bucket check =="
python3 - <<'PYEOF'
import os
import sys

import boto3
from botocore.exceptions import ClientError, BotoCoreError

endpoint = os.getenv("GARAGE_ENDPOINT_URL", "http://100.78.2.20:3900")
bucket = os.getenv("GARAGE_BUCKET", "sensor-data-staging")

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=os.getenv("GARAGE_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("GARAGE_SECRET_ACCESS_KEY"),
    region_name=os.getenv("GARAGE_REGION", "garage"),
)

try:
    client.head_bucket(Bucket=bucket)
    print(f"OK: bucket '{bucket}' reachable at {endpoint}")
except (ClientError, BotoCoreError) as exc:
    print(f"FAIL: {exc}")
    sys.exit(1)

test_key = "_garage_integration_test.txt"
client.put_object(Bucket=bucket, Key=test_key, Body=b"ok")
client.get_object(Bucket=bucket, Key=test_key)
client.delete_object(Bucket=bucket, Key=test_key)
print("OK: put/get/delete round-trip succeeded")
PYEOF

echo
echo "All Garage integration checks passed."
