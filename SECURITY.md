# Security

## Secrets
- All credentials (Garage access key/secret, AWS keys if not using an
  instance role) live in `.env`, which is gitignored. Never commit `.env`.
- On EC2, prefer the instance role created by `infra/terraform/iam.tf`
  over static AWS keys.

## Network exposure
- Garage is only reachable over the Tailscale VPN (`100.x.y.z` addresses) —
  it should never be exposed on a public IP.
- The Streamlit dashboard binds to `127.0.0.1` only (see
  `systemd/kpi-dashboard.service`). Access it via SSM Session Manager port
  forwarding, never by opening an inbound security group rule for 8501.
- The EC2 poller's security group has no inbound rules at all; management
  is via SSM, not SSH, when possible.

## Least privilege
- The EC2 instance role (`infra/terraform/iam.tf`) can only:
  - `s3:GetObject` / `s3:ListBucket` on the raw sensor bucket
  - `sqs:ReceiveMessage` / `DeleteMessage` / `GetQueueAttributes` on the one
    sensor queue
  - write CloudWatch Logs under `/sensor-kpi/*`
- It cannot write to S3, cannot touch any other queue/topic, and cannot
  perform any other AWS action.

## Data validation
- Every payload is schema-validated (`scripts/utils/schema.py`) before it
  is uploaded to AWS S3 and again before KPIs are computed, so malformed or
  malicious payloads never reach the aggregation layer.

## Reporting a vulnerability
Open a private security advisory on the repository, or contact the
maintainer directly. Do not open a public issue for undisclosed
vulnerabilities.
