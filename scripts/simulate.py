"""Offline three-turbine baseline using historical reanalysis weather.

Power curve: windpowerlib/OEDB Vestas V90/2000, used as a representative
published library model, NOT a claim about turbines at Tarfaya or any real wind farm.
Rotor RPM, component temperature and safety trip are labelled simulations.
This is the plant baseline; separate SCADA/SIS processes come in later stages.
"""

import argparse
import csv
from datetime import datetime, timezone
import math
from pathlib import Path

import pandas as pd
from windpowerlib import ModelChain, WindTurbine

ROOT = Path(__file__).resolve().parents[1]
TURBINES = {"T01": 0.98, "T02": 1.00, "T03": 1.02}
TURBINE_MODEL = "V90/2000"
HUB_HEIGHT_M = 100
TRIP_SETPOINT_C = 85.0  # SIMULATION-ONLY, never an OEM safety setting
INJECTED_BEARING_TEMP_C = 90.0  # SIMULATION-ONLY fault exercise
FIELDNAMES = (
    "timestamp_utc", "turbine_id", "wind_speed_100m_mps",
    "ambient_temperature_2m_c", "potential_power_kw", "actual_power_kw",
    "bearing_temp_c", "rotor_rpm", "operating_state", "safety_trip",
)


def load_weather(path: Path) -> list[dict]:
    """Load validated hourly weather CSV; do not invent missing measurements."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("weather CSV is empty")
    validated = []
    previous = None
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            speed = float(row["wind_speed_100m_mps"])
            temp = float(row["ambient_temperature_2m_c"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("weather CSV contains invalid measurements") from exc
        if dt.utcoffset() != timezone.utc.utcoffset(dt) or not math.isfinite(speed) or speed < 0:
            raise ValueError("invalid UTC time or wind speed")
        if not math.isfinite(temp):
            raise ValueError("invalid ambient temperature")
        if previous is not None and dt <= previous:
            raise ValueError("weather timestamps must be strictly increasing")
        previous = dt
        validated.append({"timestamp_utc": dt.isoformat().replace("+00:00", "Z"),
                          "wind_speed_100m_mps": speed,
                          "ambient_temperature_2m_c": temp})
    return validated


def simulate(weather: list[dict], overheat_turbine: str | None = None,
             overheat_at: str | None = None) -> list[dict]:
    """Deterministically calculate a sample per turbine/hour with a latched trip."""
    if not weather:
        raise ValueError("weather series is empty")
    if overheat_turbine is not None and overheat_turbine not in TURBINES:
        raise ValueError("unknown turbine ID")
    if (overheat_at is None) != (overheat_turbine is None):
        raise ValueError("provide both --overheat-turbine and --overheat-at")
    if overheat_at is not None and overheat_at not in {r["timestamp_utc"] for r in weather}:
        raise ValueError("overheat timestamp not present in weather series")

    timestamps = pd.to_datetime([row["timestamp_utc"] for row in weather], utc=True)
    turbine = WindTurbine(turbine_type=TURBINE_MODEL, hub_height=HUB_HEIGHT_M)
    rated_kw = turbine.nominal_power / 1000
    result = []
    for turbine_id, wind_factor in TURBINES.items():
        winds = [float(row["wind_speed_100m_mps"]) * wind_factor for row in weather]
        # The ±2% factors are synthetic spatial differences, not site measurements.
        weather_df = pd.DataFrame({("wind_speed", HUB_HEIGHT_M): winds}, index=timestamps)
        potential_kw = (ModelChain(turbine).run_model(weather_df).power_output / 1000).tolist()
        bearing_temp = None
        tripped = False
        for sample, wind, potential in zip(weather, winds, potential_kw, strict=True):
            ambient = float(sample["ambient_temperature_2m_c"])
            if bearing_temp is None:
                bearing_temp = ambient + 10
            load_ratio = max(0.0, min(1.0, float(potential) / rated_kw))
            normal_target = ambient + 10 + 15 * load_ratio
            bearing_temp += 0.4 * (normal_target - bearing_temp)
            if turbine_id == overheat_turbine and sample["timestamp_utc"] == overheat_at:
                bearing_temp = max(bearing_temp, INJECTED_BEARING_TEMP_C)
            if bearing_temp >= TRIP_SETPOINT_C:
                tripped = True
            if tripped:
                state, actual_kw = "SAFETY_TRIPPED", 0.0
            elif wind >= 25:
                state, actual_kw = "HIGH_WIND_CUTOUT", 0.0
            elif potential <= 0:
                state, actual_kw = "LOW_WIND", 0.0
            else:
                state, actual_kw = "RUNNING", float(potential)
            rpm = 15.0 * math.sqrt(actual_kw / rated_kw) if actual_kw else 0.0
            result.append({
                "timestamp_utc": sample["timestamp_utc"],
                "turbine_id": turbine_id,
                "wind_speed_100m_mps": round(wind, 3),
                "ambient_temperature_2m_c": ambient,
                "potential_power_kw": round(float(potential), 3),
                "actual_power_kw": round(actual_kw, 3),
                "bearing_temp_c": round(bearing_temp, 3),
                "rotor_rpm": round(rpm, 3),
                "operating_state": state,
                "safety_trip": tripped,
            })
    return sorted(result, key=lambda row: (row["timestamp_utc"], row["turbine_id"]))


def write_output(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather", type=Path, default=ROOT / "data/raw/tarfaya_weather_2024-01-01_07.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data/derived/baseline.csv")
    parser.add_argument("--overheat-turbine", choices=list(TURBINES))
    parser.add_argument("--overheat-at", help="Exact timestamp in the weather CSV, e.g. 2024-01-02T12:00:00Z")
    args = parser.parse_args()
    rows = simulate(load_weather(args.weather), args.overheat_turbine, args.overheat_at)
    write_output(rows, args.output)
    print(f"Wrote {len(rows)} turbine-hour rows: {args.output}")
    print("Power: OEDB V90/2000 library curve; weather: historical reanalysis; rotor/temperature: simulated")
    print(f"Safety trips: {sum(row['safety_trip'] for row in rows)} tripped turbine-hours")


if __name__ == "__main__":
    main()
