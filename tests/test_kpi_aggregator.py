import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.kpi_aggregator as agg  # noqa: E402


def _write_window(dir_path: Path, name: str, processed_at: str, sensor_id: str, mean: float, count: int):
    dir_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source_key": name,
        "processed_at": processed_at,
        "total_records": count,
        "invalid_records": 0,
        "sensor_count": 1,
        "sensors": {
            sensor_id: {
                "sensor_id": sensor_id,
                "count": count,
                "mean": mean,
                "min": mean - 1,
                "max": mean + 1,
                "std": 0.5,
                "rate_of_change": 0.0,
                "threshold_breaches": 0,
            }
        },
    }
    with open(dir_path / f"{name}.json", "w") as f:
        json.dump(payload, f)


def test_aggregate_once_groups_by_hour_and_day(tmp_path, monkeypatch):
    windows_dir = tmp_path / "windows"
    hourly_dir = tmp_path / "hourly"
    daily_dir = tmp_path / "daily"
    uptime_dir = tmp_path / "uptime"

    monkeypatch.setattr(agg, "KPI_WINDOWS_DIR", windows_dir)
    monkeypatch.setattr(agg, "KPI_HOURLY_DIR", hourly_dir)
    monkeypatch.setattr(agg, "KPI_DAILY_DIR", daily_dir)
    monkeypatch.setattr(agg, "KPI_UPTIME_DIR", uptime_dir)

    _write_window(windows_dir, "w1", "2026-09-11T08:05:00+00:00", "temp", 10.0, 2)
    _write_window(windows_dir, "w2", "2026-09-11T08:50:00+00:00", "temp", 20.0, 2)
    _write_window(windows_dir, "w3", "2026-09-11T09:10:00+00:00", "temp", 30.0, 2)

    n_hourly, n_daily = agg.aggregate_once()

    assert n_hourly == 2  # 08:xx and 09:xx buckets
    assert n_daily == 1   # all on the same day

    hourly_file = hourly_dir / "2026-09-11-08.json"
    assert hourly_file.exists()
    with open(hourly_file) as f:
        data = json.load(f)
    assert data["sensors"]["temp"]["count"] == 4
    assert data["window_count"] == 2

    daily_file = daily_dir / "2026-09-11.json"
    with open(daily_file) as f:
        daily_data = json.load(f)
    assert daily_data["sensors"]["temp"]["count"] == 6
    assert daily_data["window_count"] == 3


def test_aggregate_once_with_no_windows_is_noop(tmp_path, monkeypatch):
    windows_dir = tmp_path / "empty_windows"
    uptime_dir = tmp_path / "uptime"
    monkeypatch.setattr(agg, "KPI_WINDOWS_DIR", windows_dir)
    monkeypatch.setattr(agg, "KPI_UPTIME_DIR", uptime_dir)
    windows_dir.mkdir()
    uptime_dir.mkdir()

    n_hourly, n_daily = agg.aggregate_once()

    assert (n_hourly, n_daily) == (0, 0)
