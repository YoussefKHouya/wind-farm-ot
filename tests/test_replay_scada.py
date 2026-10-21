import csv
import os
import subprocess
import sys

from scripts.replay_scada import replay_scada


def observed_rows():
    return [
        {"source_turbine_id": "WT01", "timestamp_source": "2020-02-05 00:00:00",
         "Generator Bearing Temp. Avg. [°C]": "84", "Grid Production Power Avg. [W]": "123"},
        {"source_turbine_id": "WT02", "timestamp_source": "2020-02-05 00:00:00",
         "Generator Bearing Temp. Avg. [°C]": "86", "Grid Production Power Avg. [W]": "-17"},
        {"source_turbine_id": "WT02", "timestamp_source": "2020-02-05 00:10:00",
         "Generator Bearing Temp. Avg. [°C]": "50", "Grid Production Power Avg. [W]": "44"},
    ]


def test_real_measurements_remain_untouched_during_counterfactual_safety_replay():
    records = observed_rows()
    results = replay_scada(records)
    assert len(results) == len(records)
    assert results[0]["counterfactual_trip"] is False
    assert results[1]["counterfactual_trip"] is True
    assert results[1]["trip_reason"] == "BEARING_OVERTEMP"
    assert results[1]["Grid Production Power Avg. [W]"] == "-17"
    assert results[2]["counterfactual_trip"] is True  # trip latch, no invented reset
    assert results[0]["safety_service_pid"] != os.getpid()


def test_missing_recorded_sensor_is_explicitly_invalid_not_zero():
    record = observed_rows()[0]
    record["Generator Bearing Temp. Avg. [°C]"] = ""
    result = replay_scada([record])[0]
    assert result["Generator Bearing Temp. Avg. [°C]"] == ""
    assert result["counterfactual_trip"] is True
    assert result["trip_reason"] == "SENSOR_INVALID"


def test_cli_replay_sourced_csv_without_changing_power(tmp_path):
    input_file = tmp_path / "source.csv"
    with input_file.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(observed_rows()[0]))
        writer.writeheader()
        writer.writerows(observed_rows())
    output = tmp_path / "evaluated.csv"
    result = subprocess.run([sys.executable, "-m", "scripts.replay_scada", "--input", str(input_file),
                             "--output", str(output)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert b"\r" not in output.read_bytes()
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["Grid Production Power Avg. [W]"] for r in rows] == ["123", "-17", "44"]
    assert rows[1]["counterfactual_trip"] == "True"
