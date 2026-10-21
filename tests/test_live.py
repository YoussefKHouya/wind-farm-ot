import csv
import os
import subprocess
import sys

import pytest

from scripts.live import LivePlant, load_commands
from scripts.safety_service import SafetyClient


def weather(count=5, ambient=20.0):
    return [
        {"timestamp_utc": f"2024-01-01T{hour:02d}:00:00Z", "wind_speed_100m_mps": 10.0,
         "ambient_temperature_2m_c": ambient}
        for hour in range(count)
    ]


def by_id(rows, turbine_id="T02"):
    return next(row for row in rows if row["turbine_id"] == turbine_id)


def test_live_tick_uses_real_child_service_and_normal_production():
    plant = LivePlant(weather())
    with SafetyClient() as safety:
        t0 = by_id(plant.tick(0, safety))
        assert t0["operating_state"] == "RUNNING"
        assert t0["actual_power_kw"] > 0
        assert t0["safety_service_pid"] != os.getpid()


def test_overheat_trips_one_turbine_and_denies_start_until_local_reset():
    plant = LivePlant(weather())
    with SafetyClient() as safety:
        before = by_id(plant.tick(0, safety))
        tripped = by_id(plant.tick(1, safety, fault={"turbine_id": "T02", "kind": "overheat"},
                                   commands={"T02": "START"}))
        still_tripped = by_id(plant.tick(2, safety))
        reset = by_id(plant.tick(3, safety, commands={"T02": "LOCAL_RESET"}))
        restarted = by_id(plant.tick(4, safety, commands={"T02": "START"}))
    assert before["actual_power_kw"] > 0
    assert tripped["operating_state"] == "SAFETY_TRIPPED" and tripped["actual_power_kw"] == 0
    assert tripped["command_accepted"] is False
    assert still_tripped["operating_state"] == "SAFETY_TRIPPED"
    assert reset["operating_state"] == "STOPPED" and reset["command_accepted"] is True
    assert restarted["operating_state"] == "RUNNING" and restarted["actual_power_kw"] > 0
    assert all(row["operating_state"] == "RUNNING" for row in plant.history if row["turbine_id"] == "T01")


def test_hot_ambient_does_not_trip_bearing_safety_channel():
    plant = LivePlant(weather(2, ambient=40.0))
    with SafetyClient() as safety:
        rows = plant.tick(0, safety) + plant.tick(1, safety)
    assert all(row["safety_trip"] is False for row in rows)


def test_invalid_private_safety_sensor_trips_even_if_operational_telemetry_normal():
    plant = LivePlant(weather(2))
    with SafetyClient() as safety:
        plant.tick(0, safety)
        result = by_id(plant.tick(1, safety, fault={"turbine_id": "T02", "kind": "sensor_failure"}))
    assert result["safety_sensor_temp_c"] is None
    assert result["bearing_temp_c"] < 85
    assert result["safety_reason"] == "SENSOR_INVALID"
    assert result["actual_power_kw"] == 0


def test_operator_stop_and_restart_are_separate_from_safety_trip():
    plant = LivePlant(weather(3))
    with SafetyClient() as safety:
        stopped = by_id(plant.tick(0, safety, commands={"T02": "STOP"}))
        idle = by_id(plant.tick(1, safety))
        resumed = by_id(plant.tick(2, safety, commands={"T02": "START"}))
    assert stopped["operating_state"] == idle["operating_state"] == "STOPPED"
    assert stopped["safety_trip"] is False and idle["actual_power_kw"] == 0
    assert resumed["operating_state"] == "RUNNING"


def test_safety_service_crash_forces_all_turbines_to_stop():
    plant = LivePlant(weather(2))
    with SafetyClient() as safety:
        plant.tick(0, safety)
        safety.process.kill()
        safety.process.wait(timeout=3)
        rows = plant.tick(1, safety)
    assert all(row["actual_power_kw"] == 0 and row["operating_state"] == "SAFETY_UNAVAILABLE" for row in rows)


def test_cli_replay_produces_csv_using_separate_safety_process(tmp_path):
    path = tmp_path / "weather.csv"
    path.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n"
                    "2024-01-01T00:00:00Z,10,20\n2024-01-01T01:00:00Z,10,20\n")
    output = tmp_path / "live.csv"
    result = subprocess.run([sys.executable, "-m", "scripts.live", "--weather", str(path),
                             "--output", str(output), "--limit", "2",
                             "--overheat-turbine", "T02", "--overheat-at", "2024-01-01T01:00:00Z"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert b"\r" not in output.read_bytes()
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert by_id([r for r in rows if r["timestamp_utc"] == "2024-01-01T01:00:00Z"])["operating_state"] == "SAFETY_TRIPPED"


def test_commands_file_replays_denied_start_local_reset_then_restart(tmp_path):
    weather_csv = tmp_path / "weather.csv"
    weather_csv.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n" +
                           "".join(f"2024-01-01T{i:02d}:00:00Z,10,20\n" for i in range(5)))
    schedule = tmp_path / "commands.csv"
    schedule.write_text("timestamp_utc,turbine_id,command\n"
                        "2024-01-01T01:00:00Z,T02,START\n"
                        "2024-01-01T03:00:00Z,T02,LOCAL_RESET\n"
                        "2024-01-01T04:00:00Z,T02,START\n")
    output = tmp_path / "replay.csv"
    process = subprocess.run([sys.executable, "-m", "scripts.live", "--weather", str(weather_csv),
                              "--output", str(output), "--commands-file", str(schedule),
                              "--overheat-turbine", "T02", "--overheat-at", "2024-01-01T01:00:00Z"],
                             capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    t2 = [row for row in rows if row["turbine_id"] == "T02"]
    assert t2[1]["command_accepted"] == "False" and t2[1]["operating_state"] == "SAFETY_TRIPPED"
    assert t2[3]["command_accepted"] == "True" and t2[3]["operating_state"] == "STOPPED"
    assert t2[4]["command_accepted"] == "True" and t2[4]["operating_state"] == "RUNNING"


def test_commands_file_rejects_duplicate_event(tmp_path):
    file = tmp_path / "commands.csv"
    file.write_text("timestamp_utc,turbine_id,command\n"
                    "2024-01-01T01:00:00Z,T02,START\n"
                    "2024-01-01T01:00:00Z,T02,LOCAL_RESET\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_commands(file)
