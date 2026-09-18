import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.kpi_engine import compute_kpis, merge_kpi_results  # noqa: E402


def test_compute_kpis_basic_stats():
    records = [
        {"sensor_id": "temp", "value": 10},
        {"sensor_id": "temp", "value": 20},
        {"sensor_id": "temp", "value": 30},
    ]
    result = compute_kpis(records)

    stats = result.sensors["temp"]
    assert stats.count == 3
    assert stats.mean == 20
    assert stats.min_value == 10
    assert stats.max_value == 30
    assert stats.rate_of_change == 20  # last(30) - first(10)


def test_compute_kpis_skips_invalid_records():
    records = [
        {"sensor_id": "temp", "value": 10},
        {"sensor_id": "temp", "value": "oops"},
        {"value": 5},  # missing sensor_id
    ]
    result = compute_kpis(records)

    assert result.total_records == 3
    assert result.invalid_records == 2
    assert result.sensors["temp"].count == 1


def test_threshold_breaches():
    records = [
        {"sensor_id": "pressure", "value": 950},
        {"sensor_id": "pressure", "value": 1200},  # breach
        {"sensor_id": "pressure", "value": 1000},
    ]
    result = compute_kpis(records, thresholds={"pressure": (900, 1100)})

    assert result.sensors["pressure"].threshold_breaches == 1


def test_merge_kpi_results_combines_sensors():
    r1 = compute_kpis([{"sensor_id": "a", "value": 1}, {"sensor_id": "a", "value": 3}])
    r2 = compute_kpis([{"sensor_id": "a", "value": 5}])

    merged = merge_kpi_results([r1, r2])

    assert merged.sensors["a"].count == 3
    assert merged.sensors["a"].mean == 3
    assert merged.sensors["a"].min_value == 1
    assert merged.sensors["a"].max_value == 5


def test_empty_records_produce_no_sensors():
    result = compute_kpis([])
    assert result.total_records == 0
    assert result.sensors == {}
