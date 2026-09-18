# Runbook

Operational guide for the ILDS Sensor Reliability KPI Pipeline. See
[ARCHITECTURE.md](ARCHITECTURE.md) for hop/failure-mode detail and
[RELIABILITY_METRICS.md](RELIABILITY_METRICS.md) for the metrics reference.

## 0. Local / standalone

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
python3 -m pytest tests/ -q
# Standalone reliability from a local file (no AWS):
python3 scripts/reliability_cli.py data/incoming_csvs/sample_sensor_readings.json --window-minutes 40
streamlit run dashboard/app.py
```

## 1. Provision AWS with Terraform

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # edit region/bucket names
terraform init
terraform plan  -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

Creates: raw S3 bucket, SNS topic, SQS queue + DLQ (redrive policy), the S3
event notification, the EC2 instance role, and (optionally) the EC2 instance.

## 2. Configure `.env`

Copy `.env.example` → `.env` and fill in at least:
- AWS: `AWS_REGION`, `AWS_RAW_BUCKET`, `SNS_TOPIC_ARN`, `SQS_QUEUE_URL`.
- Syncthing: `SYNC_WATCH_DIR`.
- Reliability: `RELIABILITY_EXPECTED_FREQUENCY_SECONDS`,
  `RELIABILITY_HEARTBEAT_SECONDS`, `RELIABILITY_QUALITY_BOUNDS`,
  `RELIABILITY_CORRELATION_GROUPS` (see `.env.example` for the full list).

External detectors (Airtel LTE, weather, SCADA, pipeline health) are **off by
default** — only enable them when you have credentials and want the network
calls.

## 3. Run the pipeline

On the PC (or always-on box):

```bash
python3 scripts/garage_uploader.py --backfill   # stage Syncthing folder -> S3
python3 scripts/garage_sync.py                   # Garage -> AWS S3 (if used)
```

On EC2:

```bash
python3 scripts/ec2_poller.py                    # base + reliability KPIs
python3 scripts/kpi_aggregator.py --watch       # hourly/daily rollups
streamlit run dashboard/app.py                  # dashboard
```

Or via systemd:

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ec2-kpi-poller kpi-aggregator kpi-dashboard
```

## 4. Pre-flight checks

```bash
python3 scripts/aws_s3_setup_check.py     # S3/SNS/SQS/IAM wiring
python3 -m config.aws_config              # secret-free config summary
```

## 5. Logging planned maintenance

Add a window so the operational detector classifies downtime as planned and
`uptime_with_exclusions` removes it from true uptime:

```bash
python3 - <<'PY'
from scripts.utils.maintenance_calendar import get_default_calendar
get_default_calendar().add_window(
    "2026-09-11T08:20:00+00:00", "2026-09-11T08:30:00+00:00",
    "Pigging Operation", sensor_ids=["BFA8"], note="scheduled pigging")
PY
```

Or edit `data/maintenance/maintenance_windows.json` directly (see the example
in `data/maintenance/maintenance_windows.example.json`).

## 6. Accessing the dashboard (no public exposure)

```bash
aws ssm start-session \
    --target <instance-id> \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8501"],"localPortNumber":["8501"]}'
# then open http://localhost:8501
```

Never expose port 8501 publicly; the dashboard is intended to be
localhost-only on EC2.

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| No uptime reports | run `reliability_cli.py` on a sample file; check `.env` reliability vars |
| All downtime "Unknown" | expected — detectors are offline by default; supply `context` signals or enable external detectors |
| Dashboard "STALE" | check `ec2_queue_kpi_latest.json` age; verify poller + uploader + sync |
| Reliability errors in poller log | non-fatal — base KPIs still produced; check the warning in `ec2_poller.log` |
| SQS messages looping | verify the DLQ redrive policy (`aws_s3_setup_check.py`); `maxReceiveCount=5` |
