"""Tick-by-tick local replay of three simulated turbines with a child safety process.

Run from the project root: python -m scripts.live [options]
The safety process receives private simulated sensor values over a pipe. This is
logical process separation, NOT certified independence, a real SIS, or a secure
authentication boundary. CSV is evaluator truth and must not be exposed as HMI.
"""

import argparse
import csv
from datetime import datetime, timezone
import math
from pathlib import Path
import time

import pandas as pd
from windpowerlib import ModelChain, WindTurbine

from scripts.safety_service import SafetyClient
from scripts.simulate import (HUB_HEIGHT_M, INJECTED_BEARING_TEMP_C, ROOT,
                              TURBINE_MODEL, TURBINES, load_weather)

FIELDS = (
    "timestamp_utc", "turbine_id", "wind_speed_100m_mps", "ambient_temperature_2m_c",
    "potential_power_kw", "actual_power_kw", "bearing_temp_c", "safety_sensor_temp_c",
    "rotor_rpm", "operating_state", "safety_trip", "safety_reason", "safety_service_pid",
    "command_requested", "command_accepted",
)


class LivePlant:
    """Advance the process once per weather record; safety decision is external."""

    def __init__(self, weather: list[dict]):
        if not weather:
            raise ValueError("weather series is empty")
        self.weather = weather
        self.index = 0
        self.history: list[dict] = []
        self.stopped = {turbine_id: False for turbine_id in TURBINES}
        self.was_tripped = {turbine_id: False for turbine_id in TURBINES}
        self.bearing_temp = {turbine_id: None for turbine_id in TURBINES}
        model = WindTurbine(turbine_type=TURBINE_MODEL, hub_height=HUB_HEIGHT_M)
        self.rated_kw = model.nominal_power / 1000
        index = pd.to_datetime([row["timestamp_utc"] for row in weather], utc=True)
        self.curves = {}
        for turbine_id, factor in TURBINES.items():
            winds = [float(row["wind_speed_100m_mps"]) * factor for row in weather]
            df = pd.DataFrame({("wind_speed", HUB_HEIGHT_M): winds}, index=index)
            self.curves[turbine_id] = (ModelChain(model).run_model(df).power_output / 1000).tolist()

    def tick(self, index: int, safety: SafetyClient, fault: dict | None = None,
             commands: dict[str, str] | None = None) -> list[dict]:
        if index != self.index or index >= len(self.weather):
            raise ValueError("ticks must advance in order within weather data")
        if fault and (fault.get("turbine_id") not in TURBINES
                      or fault.get("kind") not in ("overheat", "sensor_failure")):
            raise ValueError("invalid fault scenario")
        commands = commands or {}
        if set(commands) - TURBINES.keys() or any(v not in ("START", "STOP", "LOCAL_RESET") for v in commands.values()):
            raise ValueError("invalid turbine command")
        sample = self.weather[index]
        ambient = float(sample["ambient_temperature_2m_c"])
        prepared = {}
        for turbine_id, factor in TURBINES.items():
            potential = max(0.0, float(self.curves[turbine_id][index]))
            prior = self.bearing_temp[turbine_id]
            if prior is None:
                prior = ambient + 10
            load_ratio = 0.0 if (self.stopped[turbine_id] or self.was_tripped[turbine_id]) else min(1.0, potential / self.rated_kw)
            target = ambient + 10 + 15 * load_ratio
            temp = prior + 0.4 * (target - prior)
            if fault and fault["turbine_id"] == turbine_id and fault["kind"] == "overheat":
                temp = max(temp, INJECTED_BEARING_TEMP_C)
            self.bearing_temp[turbine_id] = temp
            safety_sensor = None if (fault and fault["turbine_id"] == turbine_id
                                     and fault["kind"] == "sensor_failure") else temp
            prepared[turbine_id] = (float(sample["wind_speed_100m_mps"]) * factor,
                                    potential, temp, safety_sensor)

        statuses = {}
        try:
            for turbine_id in TURBINES:
                statuses[turbine_id] = safety.sample(turbine_id, prepared[turbine_id][3])
        except RuntimeError:
            # A missing protection decision is not permission to keep generating.
            statuses = {turbine_id: {"latched": True, "reason": "SAFETY_SERVICE_UNAVAILABLE",
                                     "service_pid": None} for turbine_id in TURBINES}

        rows = []
        for turbine_id in TURBINES:
            wind, potential, temp, safety_sensor = prepared[turbine_id]
            status = statuses[turbine_id]
            requested = commands.get(turbine_id)
            accepted = None
            if status["reason"] == "SAFETY_SERVICE_UNAVAILABLE":
                self.stopped[turbine_id] = True
                accepted = False if requested else None
                state, actual = "SAFETY_UNAVAILABLE", 0.0
            else:
                if requested == "LOCAL_RESET":
                    status = safety.reset(turbine_id, local_request=True)
                    accepted = status["reset_accepted"]
                    # Reset clears the trip, not the operational stop.
                    if accepted:
                        self.stopped[turbine_id] = True
                elif requested == "STOP":
                    self.stopped[turbine_id] = True
                    accepted = True
                elif requested == "START":
                    accepted = not status["latched"]
                    if accepted:
                        self.stopped[turbine_id] = False
                if status["latched"]:
                    self.stopped[turbine_id] = True
                    state, actual = "SAFETY_TRIPPED", 0.0
                elif self.stopped[turbine_id]:
                    state, actual = "STOPPED", 0.0
                elif wind >= 25:
                    state, actual = "HIGH_WIND_CUTOUT", 0.0
                elif potential <= 0:
                    state, actual = "LOW_WIND", 0.0
                else:
                    state, actual = "RUNNING", potential
            self.was_tripped[turbine_id] = bool(status["latched"])
            rows.append({
                "timestamp_utc": sample["timestamp_utc"], "turbine_id": turbine_id,
                "wind_speed_100m_mps": round(wind, 3),
                "ambient_temperature_2m_c": ambient,
                "potential_power_kw": round(potential, 3),
                "actual_power_kw": round(actual, 3),
                "bearing_temp_c": round(temp, 3),
                "safety_sensor_temp_c": round(safety_sensor, 3) if safety_sensor is not None else None,
                "rotor_rpm": round(15.0 * math.sqrt(actual / self.rated_kw), 3) if actual else 0.0,
                "operating_state": state, "safety_trip": bool(status["latched"]),
                "safety_reason": status["reason"],
                "safety_service_pid": status["service_pid"],
                "command_requested": requested, "command_accepted": accepted,
            })
        self.history.extend(rows)
        self.index += 1
        return rows


def run_replay(weather: list[dict], overheat_turbine: str | None = None,
               overheat_at: str | None = None, sensor_fail_at: str | None = None,
               stop_at: str | None = None, start_at: str | None = None,
               reset_at: str | None = None, limit: int | None = None,
               delay: float = 0.0,
               commands_schedule: dict[tuple[str, str], str] | None = None) -> list[dict]:
    """Run bounded replay; always close the local safety child process."""
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("delay must be nonnegative")
    if (overheat_turbine is None) != (overheat_at is None):
        raise ValueError("provide overheat turbine and time together")
    if (sensor_fail_at or reset_at or stop_at or start_at) and overheat_turbine is None:
        raise ValueError("specify --overheat-turbine as the target for scenario commands")
    count = len(weather) if limit is None else limit
    if count < 1 or count > len(weather):
        raise ValueError("limit must be within the weather series")
    stamps = {row["timestamp_utc"] for row in weather[:count]}
    for label, stamp in (("overheat", overheat_at), ("sensor failure", sensor_fail_at),
                         ("stop", stop_at), ("start", start_at), ("reset", reset_at)):
        if stamp is not None and stamp not in stamps:
            raise ValueError(f"{label} time not in selected weather series")
    events = [(name, timestamp) for name, timestamp in (("STOP", stop_at), ("START", start_at),
                                                        ("LOCAL_RESET", reset_at)) if timestamp]
    if len({timestamp for _, timestamp in events}) != len(events):
        raise ValueError("only one command per turbine per weather tick")
    scheduled = dict(commands_schedule or {})
    for (timestamp, turbine_id), command in scheduled.items():
        if timestamp not in stamps or turbine_id not in TURBINES or command not in ("START", "STOP", "LOCAL_RESET"):
            raise ValueError("invalid or out-of-range scheduled command")
    for command, timestamp in events:
        key = (timestamp, overheat_turbine)
        if key in scheduled:
            raise ValueError("duplicate scheduled command")
        scheduled[key] = command
    plant = LivePlant(weather[:count])
    with SafetyClient() as safety:
        for index, sample in enumerate(weather[:count]):
            fault = None
            if sample["timestamp_utc"] == overheat_at:
                fault = {"turbine_id": overheat_turbine, "kind": "overheat"}
            if sample["timestamp_utc"] == sensor_fail_at:
                fault = {"turbine_id": overheat_turbine, "kind": "sensor_failure"}
            commands = {turbine_id: name for (stamp, turbine_id), name in scheduled.items()
                        if stamp == sample["timestamp_utc"]}
            plant.tick(index, safety, fault=fault, commands=commands)
            if delay:
                time.sleep(delay)
    return plant.history


def write_live(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_commands(path: Path) -> dict[tuple[str, str], str]:
    """Read a trusted local replay fixture, not an authenticated network API."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["timestamp_utc", "turbine_id", "command"]:
            raise ValueError("commands CSV requires timestamp_utc,turbine_id,command")
        scheduled = {}
        for row in reader:
            timestamp = row["timestamp_utc"]
            turbine_id = row["turbine_id"]
            command = row["command"]
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise ValueError("invalid command timestamp") from exc
            if dt.utcoffset() != timezone.utc.utcoffset(dt) or turbine_id not in TURBINES or command not in ("START", "STOP", "LOCAL_RESET"):
                raise ValueError("invalid turbine, command, or UTC timestamp")
            key = (dt.isoformat().replace("+00:00", "Z"), turbine_id)
            if key in scheduled:
                raise ValueError("duplicate command for turbine and timestamp")
            scheduled[key] = command
    if not scheduled:
        raise ValueError("commands CSV is empty")
    return scheduled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather", type=Path, default=ROOT / "data/raw/tarfaya_weather_2024-01-01_07.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data/derived/live_baseline.csv")
    parser.add_argument("--limit", type=int, help="Replay only the first N hourly samples")
    parser.add_argument("--delay", type=float, default=0.0, help="Wall-clock seconds between simulated hourly ticks")
    parser.add_argument("--overheat-turbine", choices=list(TURBINES))
    parser.add_argument("--overheat-at", help="Synthetic bearing fault at an exact CSV timestamp (UTC)")
    parser.add_argument("--sensor-fail-at", help="Invalid private safety sensor at an exact CSV timestamp")
    parser.add_argument("--stop-at", help="Operational stop for the selected turbine")
    parser.add_argument("--start-at", help="Operational start/restart for the selected turbine")
    parser.add_argument("--reset-at", help="Trusted local reset request for the selected turbine; no auth layer yet")
    parser.add_argument("--commands-file", type=Path, help="Trusted local UTC command schedule CSV; not a network control endpoint")
    args = parser.parse_args()
    rows = run_replay(load_weather(args.weather), args.overheat_turbine, args.overheat_at,
                      args.sensor_fail_at, args.stop_at, args.start_at, args.reset_at,
                      args.limit, args.delay, load_commands(args.commands_file) if args.commands_file else None)
    write_live(rows, args.output)
    print(f"Saved {len(rows)} turbine-hour rows to {args.output}")
    print(f"Safety child PID: {rows[0]['safety_service_pid']}; trip rows: {sum(r['safety_trip'] for r in rows)}")
    for row in rows:
        if row["safety_reason"] and (row["command_requested"] or row["bearing_temp_c"] >= 85):
            print(f"{row['timestamp_utc']} {row['turbine_id']} {row['operating_state']} "
                  f"{row['safety_reason']} command={row['command_requested']} accepted={row['command_accepted']}")


if __name__ == "__main__":
    main()
