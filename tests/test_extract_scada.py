import csv
from datetime import datetime

from openpyxl import Workbook
import pytest

from scripts.extract_scada import extract_scada


FIELDS = (
    "Timestamp", "Ambient WindSpeed Avg. [m/s]", "Ambient Temp. Avg. [°C]",
    "Rotor RPM Avg. [RPM]", "Generator Bearing Temp. Avg. [°C]",
    "Generator Bearing2 Temp. Avg. [°C]", "Grid Production Power Avg. [W]",
    "Grid Production PossiblePower Avg. [W]", "System Logs First Active Alarm No",
    "missing_data",
)


def workbook(path, missing_bearing=False):
    book = Workbook()
    measurements = book.active
    measurements.title = "WT01_data.csv"
    measurements.append(list(FIELDS) if not missing_bearing else [x for x in FIELDS if x != FIELDS[4]])
    row = ["2020-01-13 00:00:00", 4.6, 8, 10, 47, 51, 143.3, 2000, 0, False]
    measurements.append(row if not missing_bearing else [x for i, x in enumerate(row) if i != 4])
    measurements.append(["2020-01-13 00:10:00", 4.1, 8, 10, None, 51, 92, 2000, 0, True] if not missing_bearing
                        else ["2020-01-13 00:10:00", 4.1, 8, 10, 51, 92, 2000, 0, True])
    measurements.append(["2020-01-14 00:00:00", 3, 8, 0, 40, 40, 0, 2000, 0, False] if not missing_bearing
                        else ["2020-01-14 00:00:00", 3, 8, 0, 40, 0, 2000, 0, False])
    logs = book.create_sheet("WT01_logs.csv")
    logs.append(["Code", "Description", "Detected", "Device ack.", "Reset/Run", "Duration", "Event type", "Severity"])
    logs.append([70, "Nacelle position reset", "2020-01-13 18:09:16", None, None, None, "System log (S)", 101])
    logs.append([71, "Second event at same instant", "2020-01-13 18:09:16", None, None, None, "System log (S)", 101])
    book.save(path)


def test_extract_preserves_measured_values_missingness_and_log(tmp_path):
    source = tmp_path / "source.xlsx"
    workbook(source)
    output = tmp_path / "measurements.csv"
    log_output = tmp_path / "logs.csv"
    counts = extract_scada(source, ("WT01",), "2020-01-13", "2020-01-13", output, log_output)
    assert counts == (2, 2)
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["source_turbine_id"] == "WT01"
    assert rows[0]["Grid Production Power Avg. [W]"] == "143.3"
    assert rows[0]["Generator Bearing Temp. Avg. [°C]"] == "47"
    assert rows[1]["Generator Bearing Temp. Avg. [°C]"] == ""
    assert rows[1]["missing_data"] == "True"
    assert rows[0]["timestamp_source"] == "2020-01-13 00:00:00"
    assert b"\r" not in output.read_bytes()
    with log_output.open(newline="") as handle:
        logs = list(csv.DictReader(handle))
    assert logs[0]["Code"] == "70" and logs[0]["Description"] == "Nacelle position reset"
    assert logs[1]["Detected"] == logs[0]["Detected"]


def test_extract_rejects_missing_required_source_field(tmp_path):
    source = tmp_path / "bad.xlsx"
    workbook(source, missing_bearing=True)
    with pytest.raises(ValueError, match="Generator Bearing Temp"):
        extract_scada(source, ("WT01",), "2020-01-13", "2020-01-13",
                      tmp_path / "data.csv", tmp_path / "logs.csv")
