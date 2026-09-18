#!/usr/bin/env bash
# Thin wrapper so systemd (or cron, or a plain shell) can launch the poller
# with the right virtualenv/interpreter and working directory, and so a
# crash triggers a clean restart via systemd's Restart= directive rather
# than silently dying.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[ec2_sqs_kpi_poller] starting from $REPO_ROOT using $PYTHON_BIN"
exec "$PYTHON_BIN" scripts/ec2_poller.py
