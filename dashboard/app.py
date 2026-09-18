#!/usr/bin/env python3
"""
Streamlit dashboard over:
  - data/kpi_reports/{windows,hourly,daily}            (base sensor KPIs)
  - data/kpi_reports/uptime/ (+ hourly/, daily/)       (reliability KPIs)
  - data/downtime/downtime_events.jsonl                 (classified downtime)
  - data/kpi_reports/ec2_queue_kpi_latest.json         (health pointer)

    streamlit run dashboard/app.py

On EC2, run as a localhost-only systemd service (systemd/kpi-dashboard.service)
and reach it via SSM port forwarding rather than exposing 8501 publicly.

The dashboard adds the reliability views from the design discourse:
  - Uptime trend (line chart) + moving-average uptime
  - Downtime root-cause breakdown (pie/bar)
  - Downtime timeline (Gantt-style table)
  - Sensor health heatmap (uptime % color-coded)
  - Multi-sensor correlation status
  - Uptime with planned/uncontrollable exclusions
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.aws_config import (  # noqa: E402
    CAUSE_LOG_PATH, KPI_DAILY_DIR, KPI_HOURLY_DIR, KPI_REPORTS_DIR,
    KPI_UPTIME_DIR, KPI_WINDOWS_DIR, MAINTENANCE_LOG_PATH,
)

st.set_page_config(page_title="Sensor Reliability KPI Dashboard", layout="wide")

UPTIME_HOURLY_DIR = KPI_UPTIME_DIR / "hourly"
UPTIME_DAILY_DIR = KPI_UPTIME_DIR / "daily"


def load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def load_dir(dir_path: Path, suffix: str = ".json") -> list[dict]:
    reports = []
    if not dir_path.exists():
        return reports
    for path in sorted(dir_path.glob(f"*{suffix}")):
        data = load_json(path)
        if data:
            data["_file"] = path.name
            reports.append(data)
    return reports


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def sensors_to_dataframe(report: dict) -> pd.DataFrame:
    sensors = report.get("sensors", {})
    if not sensors:
        return pd.DataFrame()
    rows = []
    for sensor_id, stats in sensors.items():
        row = {"sensor_id": sensor_id, **stats}
        rows.append(row)
    return pd.DataFrame(rows).set_index("sensor_id")


def uptime_sensors_to_dataframe(report: dict) -> pd.DataFrame:
    sensors = report.get("uptime", {}).get("sensors", report.get("sensors", {}))
    if not sensors:
        return pd.DataFrame()
    rows = []
    for sensor_id, stats in sensors.items():
        rows.append({"sensor_id": sensor_id, **stats})
    return pd.DataFrame(rows).set_index("sensor_id")


st.title("\U0001f4ca Sensor Reliability KPI Dashboard")
st.caption("Syncthing \u2192 Garage S3 \u2192 AWS S3 \u2192 SNS/SQS \u2192 EC2 KPI Poller \u2192 Reliability \u2192 Dashboard")

latest_path = KPI_REPORTS_DIR / "ec2_queue_kpi_latest.json"
latest = load_json(latest_path)

col1, col2, col3, col4 = st.columns(4)
if latest:
    age_seconds = time.time() - latest.get("timestamp", 0)
    stale = age_seconds > 300
    col1.metric("Last object processed", latest.get("latest_key", "\u2014"))
    col2.metric("Total records (last file)", latest.get("total_records", "\u2014"))
    col3.metric("Sensors (last file)", latest.get("sensor_count", "\u2014"))
    col4.metric("Data age", f"{age_seconds:,.0f}s", delta="STALE" if stale else "fresh", delta_color="inverse" if stale else "normal")
    if stale:
        st.warning("No new data processed in over 5 minutes \u2014 check garage_uploader.py, garage_sync.py, and the EC2 poller.")
else:
    st.info("No reports yet. Once garage_uploader.py, garage_sync.py, ec2_poller.py (or reliability_cli.py) have run, data will show up here.")

view = st.radio(
    "View",
    ["Reliability (Uptime/Downtime)", "Base KPIs (Live/Windows/Hourly/Daily)", "Downtime events", "Maintenance windows"],
    horizontal=True,
)

# --------------------------------------------------------------------------- #
# Reliability view
# --------------------------------------------------------------------------- #
if view == "Reliability (Uptime/Downtime)":
    st.header("Uptime & Downtime KPIs")
    res = st.radio("Resolution", ["Latest window", "Hourly", "Daily", "Moving average"], horizontal=True)

    if res == "Latest window":
        reports = load_dir(KPI_UPTIME_DIR, suffix=".uptime.json")
        if not reports:
            st.info("No uptime reports yet \u2014 run ec2_poller.py or scripts/reliability_cli.py.")
        else:
            report = reports[-1]
            st.caption(f"Window: {report.get('window_start')} \u2192 {report.get('window_end')}")
            df = uptime_sensors_to_dataframe(report)
            if not df.empty:
                st.subheader("Sensor uptime")
                st.dataframe(df, use_container_width=True)
                if "uptime_pct" in df.columns:
                    st.bar_chart(df["uptime_pct"])
            st.subheader("Downtime events (this window)")
            events = report.get("downtime_events", [])
            if events:
                st.dataframe(pd.DataFrame(events), use_container_width=True)
            else:
                st.success("No downtime detected in this window.")
            st.subheader("Cause breakdown")
            cb = report.get("cause_breakdown", {}).get("by_cause", {})
            if cb:
                st.bar_chart(pd.DataFrame.from_dict(cb, orient="index")["count"])
            else:
                st.info("No downtime causes to break down.")

    elif res in ("Hourly", "Daily"):
        d = UPTIME_HOURLY_DIR if res == "Hourly" else UPTIME_DAILY_DIR
        reports = load_dir(d, suffix=".uptime.json")
        if not reports:
            st.info(f"No {res.lower()} reliability rollups yet \u2014 run scripts/kpi_aggregator.py.")
        else:
            options = [r.get("bucket", r["_file"]) for r in reports]
            idx = st.selectbox(f"Choose a {res.lower()} bucket", range(len(options)), format_func=lambda i: options[i], index=len(options) - 1)
            r = reports[idx]
            st.caption(f"{r.get('window_count', 0)} window(s) aggregated")
            df = uptime_sensors_to_dataframe(r)
            st.dataframe(df, use_container_width=True)
            if not df.empty and "uptime_pct" in df.columns:
                st.bar_chart(df["uptime_pct"])
            cb = r.get("downtime_by_cause", {})
            if cb:
                st.subheader("Downtime by cause")
                cause_df = pd.DataFrame.from_dict(cb, orient="index")
                st.dataframe(cause_df, use_container_width=True)
                if "duration_seconds" in cause_df.columns:
                    st.bar_chart(cause_df["duration_seconds"])

    else:  # Moving average
        ma_files = sorted(KPI_UPTIME_DIR.glob("moving_average_*.json"))
        if not ma_files:
            st.info("No moving-average report yet \u2014 run scripts/kpi_aggregator.py.")
        else:
            ma = load_json(ma_files[-1])
            st.caption(f"Rolling horizon: {ma.get('horizon_hours')}h over {ma.get('window_count')} window(s)")
            series = ma.get("moving_average_uptime_pct", {})
            if series:
                df = pd.DataFrame.from_dict(series, orient="index", columns=["moving_average_uptime_pct"])
                st.bar_chart(df)
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No sensor uptime series available.")

    # Sensor health heatmap (uptime % color-coded).
    all_reports = load_dir(KPI_UPTIME_DIR, suffix=".uptime.json")
    if all_reports:
        st.subheader("Sensor health heatmap (latest uptime %)")
        latest_df = uptime_sensors_to_dataframe(all_reports[-1])
        if not latest_df.empty and "uptime_pct" in latest_df.columns:
            heat = latest_df[["uptime_pct"]].transpose()
            heat.columns = latest_df.index
            st.dataframe(
                heat.style.background_gradient(cmap="RdYlGn", vmin=0, vmax=100, axis=None),
                use_container_width=True,
            )

    # Multi-sensor correlation status.
    if all_reports:
        cg = all_reports[-1].get("uptime", {}).get("correlation_groups", [])
        if cg:
            st.subheader("Multi-sensor correlation")
            st.dataframe(pd.DataFrame(cg), use_container_width=True)

    # Downtime event log (all events).
    st.subheader("All classified downtime events (log)")
    events = load_jsonl(CAUSE_LOG_PATH)
    if events:
        edf = pd.DataFrame(events)
        if "cause" in edf.columns:
            st.bar_chart(edf["cause"].value_counts())
        st.dataframe(edf, use_container_width=True)
    else:
        st.info("No downtime events logged yet.")

# --------------------------------------------------------------------------- #
# Base KPI view
# --------------------------------------------------------------------------- #
elif view == "Base KPIs (Live/Windows/Hourly/Daily)":
    resolution = st.radio("Report resolution", ["Live (last window)", "Windows", "Hourly", "Daily"], horizontal=True)
    if resolution == "Live (last window)":
        if latest and latest.get("window_file"):
            report = load_json(KPI_WINDOWS_DIR / latest["window_file"])
        else:
            windows = load_dir(KPI_WINDOWS_DIR)
            report = windows[-1] if windows else None
        if report:
            st.subheader(f"Window: {report.get('source_key', report.get('_file', ''))}")
            st.dataframe(sensors_to_dataframe(report), use_container_width=True)
        else:
            st.info("No window reports yet.")
    elif resolution == "Windows":
        windows = load_dir(KPI_WINDOWS_DIR)
        if not windows:
            st.info("No window reports yet.")
        else:
            options = [w.get("source_key", w["_file"]) for w in windows]
            idx = st.selectbox("Choose a window", range(len(options)), format_func=lambda i: options[i], index=len(options) - 1)
            st.dataframe(sensors_to_dataframe(windows[idx]), use_container_width=True)
    elif resolution == "Hourly":
        hourly = load_dir(KPI_HOURLY_DIR)
        if not hourly:
            st.info("No hourly rollups yet \u2014 run scripts/kpi_aggregator.py.")
        else:
            options = [h["bucket"] for h in hourly]
            idx = st.selectbox("Choose an hour", range(len(options)), format_func=lambda i: options[i], index=len(options) - 1)
            st.caption(f"{hourly[idx]['window_count']} window(s) aggregated")
            st.dataframe(sensors_to_dataframe(hourly[idx]), use_container_width=True)
    else:
        daily = load_dir(KPI_DAILY_DIR)
        if not daily:
            st.info("No daily rollups yet \u2014 run scripts/kpi_aggregator.py.")
        else:
            options = [d["bucket"] for d in daily]
            idx = st.selectbox("Choose a day", range(len(options)), format_func=lambda i: options[i], index=len(options) - 1)
            st.caption(f"{daily[idx]['window_count']} window(s) aggregated")
            df = sensors_to_dataframe(daily[idx])
            st.dataframe(df, use_container_width=True)
            if not df.empty and "mean" in df.columns:
                st.bar_chart(df["mean"])

# --------------------------------------------------------------------------- #
# Downtime events view
# --------------------------------------------------------------------------- #
elif view == "Downtime events":
    st.header("Classified downtime events")
    events = load_jsonl(CAUSE_LOG_PATH)
    if not events:
        st.info("No downtime events logged. Run the poller or reliability_cli.py.")
    else:
        edf = pd.DataFrame(events)
        if "start" in edf.columns:
            edf["start"] = pd.to_datetime(edf["start"], errors="coerce")
        if "end" in edf.columns:
            edf["end"] = pd.to_datetime(edf["end"], errors="coerce")
        st.subheader("Root-cause breakdown")
        if "cause" in edf.columns:
            st.bar_chart(edf["cause"].value_counts())
        st.subheader("Timeline (Gantt-style)")
        st.dataframe(edf, use_container_width=True)
        if {"start", "end", "duration_seconds"}.issubset(edf.columns):
            st.subheader("Downtime duration over time")
            timeline = edf.dropna(subset=["start"]).set_index("start")["duration_seconds"]
            st.line_chart(timeline)

# --------------------------------------------------------------------------- #
# Maintenance windows view
# --------------------------------------------------------------------------- #
else:
    st.header("Planned maintenance windows")
    windows = load_json(MAINTENANCE_LOG_PATH)
    if not windows:
        st.info("No maintenance windows registered. Add them via the MaintenanceCalendar API or data/maintenance/maintenance_windows.json.")
    else:
        st.dataframe(pd.DataFrame(windows), use_container_width=True)

st.divider()
st.caption("Data directories: " + str(KPI_REPORTS_DIR) + " | " + str(CAUSE_LOG_PATH))
