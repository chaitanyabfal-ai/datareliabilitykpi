# ILDS Sensor Reliability KPI Pipeline — Syncthing → Garage S3 → AWS S3 → SQS → EC2 → Reliability → Dashboard

This project is a **full-fledged data-reliability KPI pipeline** built on top of the
[`kpidataonprempcs3`](https://github.com/chaitanyabfal-ai/kpidataonprempcs3) ingestion
pipeline. It keeps the complete end-to-end data path from that basis repo
(Syncthing → Garage S3 → AWS S3 → SNS/SQS → EC2 → Streamlit) and adds a
comprehensive **uptime/downtime reliability layer** for oil-pipeline sensor devices,
implemented as **rule-based metrics — no machine learning**.

The reliability layer implements the uptime and downtime approaches from the
ILDS (Intelligent Leak Detection System) reliability design: timestamp
consonance, differ rate, heartbeat monitoring, moving-average uptime,
data-quality uptime, multi-sensor correlation, and a multi-category downtime
root-cause classifier (network, hardware, software, operational, environmental,
cybersecurity, data-pipeline, human error).

## What this adds on top of the basis repo

| Area | Basis repo (`kpidataonprempcs3`) | This project |
|---|---|---|
| Reliability KPIs | none | full uptime/downtime engine (rule-based, no ML) |
| Uptime metrics | — | timestamp consonance, differ rate, heartbeat, moving average, data-quality, multi-sensor correlation |
| Downtime root cause | — | 8-category decision-tree classifier with injectable detectors |
| Cause detectors | — | Airtel LTE, connectivity, hardware (battery/SIM/voltage/clock-drift), software, operational (maintenance calendar), environmental, cybersecurity, pipeline (SQS/S3/SNS), human error |
| Planned-downtime handling | — | maintenance calendar store; planned + uncontrollable causes excluded from true uptime |
| Aggregation | KPI hourly/daily | KPI **and** reliability hourly/daily rollups + moving-average horizon |
| Dashboard | base KPI tabs | + reliability view (uptime trend, downtime breakdown, timeline, sensor health heatmap, correlation), downtime events view, maintenance windows view |
| Standalone use | — | `scripts/reliability_cli.py` computes reliability from a local CSV/JSON with no AWS |
| Tests | KPI + uploader | + uptime engine, downtime classifier, reliability reporter, downtime event log (47 tests) |

## Architecture

```
On-Prem Server (sensor writer)
    │  writes <name>.tmp -> atomic rename -> <name>.json/.csv
    ▼
Syncthing (Send Only) ─────────────►  Syncthing (Receive Only, your PC)
                                              │
                                              ▼
                                   scripts/garage_uploader.py
                                              │  boto3 PUT (Tailscale)
                                              ▼
                                   Garage S3  (sensor-data-staging)
                                              │
                                   scripts/garage_sync.py
                                              │  boto3 PUT
                                              ▼
                                   AWS S3  (raw-sensor-data/)
                                              │
                                   S3 ObjectCreated:* event
                                              ▼
                                        SNS Topic
                                              ▼
                                        SQS Queue  (+ DLQ)
                                              │
                                   scripts/ec2_poller.py
                                   ├──► data/kpi_reports/windows/*.json      (per-file KPI window)
                                   ├──► data/kpi_reports/uptime/*.uptime.json  (reliability: uptime/downtime)
                                   ├──► data/downtime/downtime_events.jsonl    (classified downtime events)
                                   └──► data/kpi_reports/ec2_queue_kpi_latest.json
                                              │
                                   scripts/kpi_aggregator.py
                                   ├──► data/kpi_reports/hourly/*.json
                                   ├──► data/kpi_reports/daily/*.json
                                   └──► data/kpi_reports/uptime/{hourly,daily}/*.uptime.json  + moving_average_*h.json
                                              │
                                   dashboard/app.py (Streamlit)
```

## Repository layout

```
datareliabilitykpi/
├── README.md
├── ARCHITECTURE.md
├── RUNBOOK.md
├── RELIABILITY_METRICS.md          # uptime/downtime metrics reference
├── SYNCTHING_SETUP.md
├── SECURITY.md
├── .env.example
├── requirements.txt
├── config/
│   └── aws_config.py               # base + ReliabilityConfig (cadence, heartbeat, detectors...)
├── scripts/
│   ├── garage_uploader.py          # Syncthing folder -> Garage/AWS S3   (carried over)
│   ├── garage_sync.py             # Garage S3 -> AWS S3                  (carried over)
│   ├── s3_uploader.py             # schema validation + upload helper    (carried over)
│   ├── aws_s3_setup_check.py      # AWS preflight checks                  (carried over)
│   ├── ec2_poller.py              # SQS consumer + KPI + reliability      (extended)
│   ├── kpi_aggregator.py         # KPI + reliability rollups             (extended)
│   ├── reliability_cli.py        # STANDALONE offline reliability entry point (NEW)
│   └── utils/
│       ├── kpi_engine.py         # base KPI stats engine                 (carried over)
│       ├── uptime_engine.py      # uptime KPIs (consonance/differ/heartbeat/...)  (NEW)
│       ├── downtime_classifier.py# 8-category decision-tree classifier   (NEW)
│       ├── reliability_reporter.py# uptime + downtime orchestration      (NEW)
│       ├── maintenance_calendar.py# planned maintenance windows store    (NEW)
│       ├── downtime_event_log.py # append-only classified-downtime log   (NEW)
│       ├── schema.py             # schema + telemetry forwarding          (extended)
│       ├── state_store.py        # idempotency store                     (carried over)
│       ├── logging_config.py     # shared logging                        (carried over)
│       └── detectors/
│           ├── detectors.py      # rule-based cause detectors            (NEW)
│           └── __init__.py
├── dashboard/
│   └── app.py                     # Streamlit: reliability + base KPI + downtime + maintenance (extended)
├── infra/terraform/               # S3/SNS/SQS/EC2/IAM                    (carried over)
├── systemd/                       # services incl. kpi-aggregator         (extended)
├── data/                          # windows/hourly/daily/uptime/downtime/maintenance/state
├── tests/                         # 47 tests
└── notes/
```

## Quick start

### 0. Install dependencies

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env   # edit: AWS, reliability tuning
```

### 1. Standalone reliability (no AWS needed)

Validate the uptime/downtime logic against a local sensor file before wiring
it into the cloud pipeline:

```bash
export RELIABILITY_EXPECTED_FREQUENCY_SECONDS=300
export RELIABILITY_HEARTBEAT_SECONDS=600
export RELIABILITY_QUALITY_BOUNDS="BFA8:0:100,BFA3:0:100"
export RELIABILITY_CORRELATION_GROUPS="BFA8,BFA3"
python3 scripts/reliability_cli.py data/incoming_csvs/sample_sensor_readings.json --window-minutes 40 --no-write
```

To persist reports instead of printing:

```bash
python3 scripts/reliability_cli.py data/incoming_csvs/sample_sensor_readings.json --window-minutes 40
# -> data/kpi_reports/uptime/<ts>.uptime.json
# -> data/downtime/downtime_events.jsonl
```

### 2. On-prem + PC: Syncthing

See **[SYNCTHING_SETUP.md](SYNCTHING_SETUP.md)** for the full walkthrough.

### 3. PC: stage sensor files into Garage/AWS S3

```bash
python3 scripts/garage_uploader.py            # continuous
python3 scripts/garage_uploader.py --backfill # push existing files, then continue
```

### 4. PC or always-on box: Garage → AWS S3

```bash
python3 scripts/garage_sync.py           # continuous
python3 scripts/garage_sync.py --once     # one-shot for cron/testing
```

### 5. AWS: provision S3 → SNS → SQS → EC2 fan-out

```bash
cd infra/terraform
terraform init
terraform apply -var-file=terraform.tfvars
```

### 6. EC2: poller + aggregator + dashboard

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ec2-kpi-poller.service kpi-aggregator.service kpi-dashboard.service
```

Or by hand for testing:

```bash
python3 scripts/ec2_poller.py
python3 scripts/kpi_aggregator.py --watch
streamlit run dashboard/app.py
```

For remote access without exposing Streamlit publicly:

```bash
aws ssm start-session \
    --target <instance-id> \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8501"],"localPortNumber":["8501"]}'
```

## Reliability metrics (summary)

See **[RELIABILITY_METRICS.md](RELIABILITY_METRICS.md)** for the full reference.

**Uptime approaches (all rule-based, no ML):**
1. Timestamp consonance — delay between sensor timestamp and ingestion timestamp.
2. Differ rate — actual vs expected data points in a window.
3. Heartbeat monitoring — no data within heartbeat interval ⇒ down.
4. Moving-average uptime — rolling-average smoothing across sub-windows.
5. Data-quality uptime — only in-range records count.
6. Multi-sensor correlation — corroborate sensors monitoring the same segment.

**Downtime root-cause categories (decision tree, in priority order):**
network → hardware → software → operational → environmental → cybersecurity → pipeline → human → unknown.

Each cause has an injectable, offline-safe detector in
`scripts/utils/detectors/`. External detectors (Airtel LTE API, weather,
SCADA, AWS pipeline health) are **disabled by default** and only touch the
network when their `*_ENABLED` flag is set, so the pipeline runs offline by
default. The maintenance calendar (`data/maintenance/maintenance_windows.json`)
lets operators log planned windows that are excluded from true uptime.

## Runbook

See [RUNBOOK.md](RUNBOOK.md) for AWS CLI/Terraform provisioning steps,
and [ARCHITECTURE.md](ARCHITECTURE.md) for each hop and its failure modes.

## Tests

```bash
python3 -m pytest tests/ -q
```

Covers the base KPI engine, the uptime engine, the downtime classifier and all
detectors, the reliability reporter, the downtime event log, the aggregator,
and the uploader contract.
