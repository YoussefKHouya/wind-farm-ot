"""Counterfactual safety assessment of published, measured SCADA records.

This does NOT send commands to a turbine or change recorded power. Published
SCADA bearing temperature is not an independent safety sensor. Trip setpoint
is an illustrative value, not the source wind farm's equipment setting.
"""

import argparse
import csv
from datetime import datetime
from pathlib import Path

from scripts.safety_service import SafetyClient

ROOT = Path(__file__).resolve().parents[1]
TEMP_FIELD = "Generator Bearing Temp. Avg. [°C]"
INPUT_DEFAULT = ROOT / "data/raw/greek_scada_3t_2020-02-05.csv"
OUTPUT_DEFAULT = ROOT / "data/derived/greek_scada_safety_counterfactual_2020-02-05.csv"
ADDED_FIELDS = ("counterfactual_trip", "trip_reason", "safety_service_pid")


def replay_scada(records: list[dict]) -> list[dict]:
    """Chronological shared-process replay, retaining original row order/values."""
    annotated: list[dict | None] = [None] * len(records)
    times = {}
    for index, record in enumerate(records):
        turbine_id = record["source_turbine_id"]
        if turbine_id not in ("WT01", "WT02", "WT03"):
            raise ValueError(f"unknown replay turbine {turbine_id!r}")
        stamp = datetime.fromisoformat(record["timestamp_source"])
        key = (turbine_id, stamp)
        if key in times:
            raise ValueError(f"duplicate turbine timestamp: {key!r}")
        times[key] = index
    with SafetyClient() as safety:
        for (turbine_id, _timestamp), index in sorted(times.items(), key=lambda item: (item[0][1], item[0][0])):
            record = records[index]
            raw_temp = record[TEMP_FIELD]
            try:
                temp = float(raw_temp) if raw_temp not in ("", None) else None
            except (ValueError, TypeError):
                temp = None
            # The generic simulated safety process uses T01-T03 identifiers. This is
            # only a replay mapping; it is not a link to the Greek farm controls.
            decision = safety.sample("T" + turbine_id[2:], temp)
            annotated[index] = {
                **record, "counterfactual_trip": decision["latched"],
                "trip_reason": decision["reason"] or "",
                "safety_service_pid": decision["service_pid"],
            }
    return [record for record in annotated if record is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT_DEFAULT)
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    args = parser.parse_args()
    with args.input.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"source_turbine_id", "timestamp_source", TEMP_FIELD}.issubset(reader.fieldnames):
            parser.error("input missing turbine ID, source timestamp, or bearing temperature")
        if set(ADDED_FIELDS).intersection(reader.fieldnames):
            parser.error("input already contains derived safety columns")
        original_fields = reader.fieldnames
        records = list(reader)
    if not records:
        parser.error("input has no measurement rows")
    evaluated = replay_scada(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*original_fields, *ADDED_FIELDS], lineterminator="\n")
        writer.writeheader()
        writer.writerows(evaluated)
    print(f"Assessed {len(evaluated)} published SCADA rows -> {args.output}")
    print("Counterfactual safety decision only: no physical trip, no source-data changes, no independent SIS sensor")


if __name__ == "__main__":
    main()
