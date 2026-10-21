"""Stream real ten-turbine SCADA measurements/logs from a public Zenodo XLSX.

Source: Zenodo record 14546480 (Greek wind farm, Jan-Jun 2020, CC BY 4.0).
Output retains source column labels/units and source timestamps *without inventing
UTC*. No conversion of suspiciously-labelled power units is performed.
This is genuine published SCADA replay data, not site telemetry.
"""

import argparse
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/external/SCADA__monitoring_dataset_2020.xlsx"
SOURCE_FIELDS = (
    "Ambient WindSpeed Avg. [m/s]", "Ambient Temp. Avg. [°C]",
    "Rotor RPM Avg. [RPM]", "Generator Bearing Temp. Avg. [°C]",
    "Generator Bearing2 Temp. Avg. [°C]", "Grid Production Power Avg. [W]",
    "Grid Production PossiblePower Avg. [W]", "System Logs First Active Alarm No",
    "missing_data",
)
LOG_FIELDS = ("Code", "Description", "Detected", "Device ack.", "Reset/Run",
              "Duration", "Event type", "Severity")


def _time(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        raise ValueError(f"invalid source timestamp: {value!r}")
    return datetime.fromisoformat(value)


def _stream_sheet(sheet, source_fields, time_field, turbine_id, start, end, writer) -> int:
    rows = sheet.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration as exc:
        raise ValueError(f"empty source sheet {sheet.title}") from exc
    mapping = {name: index for index, name in enumerate(header)}
    for name in source_fields:
        if name not in mapping:
            raise ValueError(f"missing source field {name} in {sheet.title}")
    count = 0
    previous = None
    for values in rows:
        timestamp = values[mapping[time_field]]
        if timestamp is None:
            continue
        dt = _time(timestamp)
        if previous is not None and (dt < previous or (dt == previous and time_field == "Timestamp")):
            raise ValueError(f"non-increasing source timestamp in {sheet.title}: {timestamp}")
        previous = dt
        if dt < start:
            continue
        if dt >= end:
            break
        record = {name: values[mapping[name]] for name in source_fields}
        if time_field == "Timestamp":
            record.pop("Timestamp")
            record = {"source_turbine_id": turbine_id, "timestamp_source": str(timestamp), **record}
        else:
            record = {"source_turbine_id": turbine_id, **record}
        writer.writerow(record)
        count += 1
    return count


def extract_scada(source: Path, turbine_ids: tuple[str, ...], start_day: str, end_day: str,
                  output: Path, logs_output: Path) -> tuple[int, int]:
    if not turbine_ids or len(set(turbine_ids)) != len(turbine_ids):
        raise ValueError("choose unique turbine IDs")
    if any(t not in {f"WT{i:02d}" for i in range(1, 11)} for t in turbine_ids):
        raise ValueError("unknown source turbine ID")
    start = datetime.combine(date.fromisoformat(start_day), datetime.min.time())
    end = datetime.combine(date.fromisoformat(end_day) + timedelta(days=1), datetime.min.time())
    if end <= start:
        raise ValueError("end date before start date")
    book = load_workbook(source, read_only=True, data_only=True)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        logs_output.parent.mkdir(parents=True, exist_ok=True)
        data_count = log_count = 0
        with output.open("w", newline="", encoding="utf-8") as data_handle, \
                logs_output.open("w", newline="", encoding="utf-8") as log_handle:
            data_writer = csv.DictWriter(data_handle,
                                         fieldnames=("source_turbine_id", "timestamp_source", *SOURCE_FIELDS),
                                         lineterminator="\n")
            log_writer = csv.DictWriter(log_handle,
                                        fieldnames=("source_turbine_id", *LOG_FIELDS),
                                        lineterminator="\n")
            data_writer.writeheader()
            log_writer.writeheader()
            for turbine_id in turbine_ids:
                data_name, logs_name = f"{turbine_id}_data.csv", f"{turbine_id}_logs.csv"
                if data_name not in book or logs_name not in book:
                    raise ValueError(f"source workbook missing turbine sheets for {turbine_id}")
                data_count += _stream_sheet(book[data_name], ("Timestamp", *SOURCE_FIELDS),
                                            "Timestamp", turbine_id, start, end, data_writer)
                log_count += _stream_sheet(book[logs_name], LOG_FIELDS, "Detected",
                                           turbine_id, start, end, log_writer)
    finally:
        book.close()
    return data_count, log_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--turbines", nargs="+", default=["WT01", "WT02", "WT03"])
    parser.add_argument("--start", default="2020-01-13")
    parser.add_argument("--end", default="2020-01-14")
    parser.add_argument("--output", type=Path, default=ROOT / "data/raw/greek_scada_3t_2020-01-13_14.csv")
    parser.add_argument("--logs-output", type=Path, default=ROOT / "data/raw/greek_logs_3t_2020-01-13_14.csv")
    args = parser.parse_args()
    counts = extract_scada(args.source, tuple(args.turbines), args.start, args.end,
                           args.output, args.logs_output)
    print(f"Preserved {counts[0]} genuine source SCADA measurements -> {args.output}")
    print(f"Preserved {counts[1]} genuine source event logs -> {args.logs_output}")
    print("Source timestamps have unspecified timezone; power units preserved exactly as printed in workbook")


if __name__ == "__main__":
    main()
