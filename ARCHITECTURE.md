# Architecture

## Hops and failure modes

This extends the `kpidataonprempcs3` architecture with the reliability
(uptime/downtime) layer. Base KPI hops are unchanged; the reliability layer
runs alongside them.

### 1. On-prem sensor → Syncthing → PC
- **Transport**: Syncthing, LAN or Tailscale between on-prem and PC.
- **Failure mode**: on-prem process crashes mid-write → mitigated by atomic
  temp-file + rename (see `SYNCTHING_SETUP.md`).
- **Failure mode**: PC offline → Syncthing queues on the server (Send Only)
  and catches up on reconnect; no data loss as long as the server disk doesn't
  fill up first.

### 2. PC → Garage/AWS S3 (`garage_uploader.py`)
- `watchdog` filesystem events → boto3 S3 API over Tailscale.
- Idempotent via a JSON state store keyed by `(filename, size, mtime)`.
- Retries failed uploads with exponential backoff; deletes local file only
  after a confirmed `put_object` (when `DELETE_AFTER_UPLOAD=true`).

### 3. Garage S3 → AWS S3 (`garage_sync.py`)
- Polls Garage for new/changed objects, validates against the shared schema,
  uploads to AWS S3 under `raw-sensor-data/<YYYY>/<MM>/<DD>/<file>`.
- Tracks synced keys in a local state file so `--once` runs are idempotent.

### 4. AWS S3 → SNS → SQS
- S3 `ObjectCreated:*` publishes to an SNS topic; SNS fans out to an SQS
  queue with a dead-letter queue (`maxReceiveCount = 5`). Multiple SQS
  subscribers can be added without touching S3.

### 5. EC2 poller (`ec2_poller.py`) — base KPIs + reliability
- Long-polls SQS, unwraps the SNS envelope, fetches each S3 object, and:
  - computes base KPIs (`kpi_engine.py`) → `data/kpi_reports/windows/*.json`;
  - computes **reliability KPIs** (`reliability_reporter.py`) for the file's
    time span → `data/kpi_reports/uptime/*.uptime.json` and appends classified
    downtime events to `data/downtime/downtime_events.jsonl`.
- Reliability reporting is wrapped so a failure there never blocks the base
  KPI pipeline (it logs a warning and continues).
- Deletes the SQS message only after the window file is durably written;
  re-processing the same key just overwrites the same window file (idempotent).

### 6. Reliability layer (`scripts/utils/`)
- **`uptime_engine.py`** — pure-Python uptime KPIs: timestamp consonance,
  differ rate, heartbeat, data-quality, multi-sensor correlation, moving
  average, and uptime-with-exclusions. No ML, no network.
- **`downtime_classifier.py`** — the 8-category decision tree. Detectors are
  injected; the classifier walks `network → hardware → software → operational
  → environmental → cybersecurity → pipeline → human → unknown`.
- **`detectors/detectors.py`** — the rule-based detectors. External detectors
  (Airtel LTE, weather, SCADA, AWS pipeline health) are gated behind
  `*_ENABLED` flags and return `None` (unknown) when disabled, so the pipeline
  runs offline by default.
- **`maintenance_calendar.py`** — planned maintenance windows persisted as
  JSON; cross-referenced by the operational detector and excluded from true
  uptime by `uptime_with_exclusions`.
- **`downtime_event_log.py`** — append-only JSONL of classified downtime
  events, read by the dashboard.
- **`reliability_reporter.py`** — detects downtime gaps from each sensor's
  timeline, classifies them, logs events, and writes the reliability report.

### 7. KPI aggregator (`kpi_aggregator.py`)
- Rolls per-file KPI windows up into hourly/daily summaries (unchanged).
- **Also** rolls per-file reliability reports up into hourly/daily reliability
  rollups (`data/kpi_reports/uptime/{hourly,daily}/*.uptime.json`) and a
  moving-average horizon report (`moving_average_<H>h.json`).
- Uses identical formulas to the per-file layer so numbers never drift.

### 8. Dashboard (`dashboard/app.py`)
- Streamlit app with four views:
  - **Reliability (Uptime/Downtime)** — latest window / hourly / daily /
    moving average, sensor uptime, downtime events, cause breakdown, sensor
    health heatmap (uptime % color-coded), multi-sensor correlation.
  - **Base KPIs** — Live / Windows / Hourly / Daily (unchanged).
  - **Downtime events** — root-cause breakdown + Gantt-style timeline.
  - **Maintenance windows** — planned windows registry.
- Runs as a localhost-only systemd service on EC2; access via SSM port
  forwarding, never expose 8501 publicly.

## Security notes

- Garage and AWS credentials are read from environment variables / `.env`
  (see `.env.example`), never hard-coded.
- The EC2 instance role is scoped to `s3:GetObject` on the raw bucket,
  `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on the one queue,
  and nothing else (`infra/terraform/iam.tf`).
- Reliability detectors never perform active security scans or network calls
  unless their `*_ENABLED` flag is set; the offline default is the safe mode.
- See `SECURITY.md` for the full policy.
