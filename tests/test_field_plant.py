"""Level 0 field process: turbines, motorised breakers, export meter, safety and local console."""
import json
import math
import os
import socket
import stat
import subprocess
import sys
import time

import pytest

from scripts.field_plant import (Breaker, FEEDER_BREAKER_MAX_A, TURBINE_BREAKER_MAX_A,
                                 TRANSFORMER_EFFICIENCY, WindFarmField)
from scripts.safety_service import SafetyClient
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")


def weather(rows=8, wind=10.0, ambient=20.0):
    return [{"timestamp_utc": f"2024-01-01T{hour:02d}:00:00Z",
             "wind_speed_100m_mps": wind, "ambient_temperature_2m_c": ambient}
            for hour in range(rows)]


def all_closed():
    return {"run": {t: True for t in TURBINES},
            "breaker": {**{t: 1 for t in TURBINES}, "FDR": 1}}


@pytest.fixture
def field():
    with SafetyClient() as safety:
        yield WindFarmField(weather(), safety)


# --- breaker -----------------------------------------------------------------------------

def test_breaker_closes_on_rising_pulse_and_opens_on_falling_pulse():
    breaker = Breaker("CB-T01", max_current_a=100)
    assert breaker.closed is False
    breaker.set_motor(1)
    assert breaker.closed is True
    breaker.set_motor(1)  # no edge, no change
    assert breaker.closed is True
    breaker.set_motor(0)
    assert breaker.closed is False


def test_breaker_overcurrent_trip_leaves_motor_coil_high():
    breaker = Breaker("CB-T01", max_current_a=100)
    breaker.set_motor(1)
    assert breaker.carry(99.0) is True
    assert breaker.carry(100.0) is False
    assert breaker.closed is False and breaker.motor == 1 and breaker.trip_reason == "OVERCURRENT"
    breaker.set_motor(1)
    assert breaker.closed is False  # reclose needs a fresh 0 -> 1 pulse
    breaker.set_motor(0)
    breaker.set_motor(1)
    assert breaker.closed is True and breaker.trip_reason is None


def test_blocked_breaker_refuses_reclose():
    breaker = Breaker("CB-T02", max_current_a=100)
    breaker.blocked = True
    breaker.set_motor(1)
    assert breaker.closed is False


# --- electrical flow ---------------------------------------------------------------------

def test_initial_field_is_de_energised(field):
    snap = field.snapshot()
    assert all(not closed for closed in snap["sensors"]["breaker_closed"].values())
    assert snap["meters"]["export_kw"] == 0
    assert set(snap["sensors"]["turbine_state"].values()) == {"STOPPED"}


def test_running_turbines_feed_export_meter_through_transformer(field):
    field.apply_outputs(all_closed())
    snap = field.step()
    meters = snap["meters"]
    produced = sum(meters["turbines"][t]["power_kw"] for t in TURBINES)
    assert produced > 0
    assert set(snap["sensors"]["turbine_state"].values()) == {"RUNNING"}
    assert meters["export_kw"] == pytest.approx(produced * TRANSFORMER_EFFICIENCY, rel=1e-6)
    assert meters["feeder"]["kv"] == 33.0
    expected_a = meters["export_kw"] * 1000 / (math.sqrt(3) * 33_000 * 0.95)
    assert meters["feeder"]["current_a"] == pytest.approx(expected_a, rel=1e-6)
    for t in TURBINES:
        turbine = meters["turbines"][t]
        assert turbine["gen_kv"] == 0.69
        assert turbine["downstream_a"] == pytest.approx(turbine["gen_a"])
        assert 0 < turbine["gen_a"] < TURBINE_BREAKER_MAX_A
    assert meters["feeder"]["current_a"] < FEEDER_BREAKER_MAX_A


def test_open_turbine_breaker_disconnects_only_that_turbine(field):
    outputs = all_closed()
    field.apply_outputs(outputs)
    field.step()
    outputs["breaker"]["T02"] = 0
    field.apply_outputs(outputs)
    snap = field.step()
    assert snap["sensors"]["turbine_state"]["T02"] == "DISCONNECTED"
    assert snap["meters"]["turbines"]["T02"]["downstream_a"] == 0
    assert snap["meters"]["turbines"]["T01"]["power_kw"] > 0


def test_open_feeder_is_grid_loss_and_zero_export(field):
    outputs = all_closed()
    field.apply_outputs(outputs)
    field.step()
    outputs["breaker"]["FDR"] = 0
    field.apply_outputs(outputs)
    snap = field.step()
    assert set(snap["sensors"]["turbine_state"].values()) == {"GRID_LOSS"}
    assert snap["meters"]["export_kw"] == 0 and snap["meters"]["feeder"]["current_a"] == 0


def test_stop_request_stops_turbine(field):
    outputs = all_closed()
    field.apply_outputs(outputs)
    field.step()
    outputs["run"]["T03"] = False
    field.apply_outputs(outputs)
    snap = field.step()
    assert snap["sensors"]["turbine_state"]["T03"] == "STOPPED"
    assert snap["meters"]["turbines"]["T03"]["power_kw"] == 0
    assert snap["meters"]["turbines"]["T03"]["rpm"] == 0


def test_high_wind_cutout():
    with SafetyClient() as safety:
        plant = WindFarmField(weather(wind=30.0), safety)
        plant.apply_outputs(all_closed())
        snap = plant.step()
    assert set(snap["sensors"]["turbine_state"].values()) == {"HIGH_WIND_CUTOUT"}
    assert snap["meters"]["export_kw"] == 0


def test_overcurrent_fault_trips_turbine_breaker_with_coil_still_high(field):
    field.apply_outputs(all_closed())
    field.step()
    field.inject_fault("T01", "overcurrent")
    snap = field.step()
    assert snap["sensors"]["breaker_closed"]["T01"] is False
    assert snap["sensors"]["breaker_trip_reason"]["T01"] == "OVERCURRENT"
    assert field.breakers["T01"].motor == 1
    assert snap["sensors"]["turbine_state"]["T01"] == "DISCONNECTED"


def test_outputs_are_validated(field):
    with pytest.raises(ValueError):
        field.apply_outputs({"run": {"T09": True}, "breaker": {}})
    with pytest.raises(ValueError):
        field.apply_outputs({"run": {}, "breaker": {"FDR": 2}})
    with pytest.raises(ValueError):
        field.inject_fault("T01", "teleport")


# --- safety --------------------------------------------------------------------------------

def test_overheat_trips_safety_opens_breaker_and_denies_restart(field):
    outputs = all_closed()
    field.apply_outputs(outputs)
    field.step()
    field.inject_fault("T02", "overheat")
    snap = field.step()
    assert snap["sensors"]["safety_latched"]["T02"] is True
    assert snap["sensors"]["safety_reason"]["T02"] == "BEARING_OVERTEMP"
    assert snap["sensors"]["turbine_state"]["T02"] == "SAFETY_TRIPPED"
    assert snap["sensors"]["breaker_closed"]["T02"] is False
    assert snap["sensors"]["breaker_trip_reason"]["T02"] == "SAFETY_TRIP"
    # PLC cycles the closer and keeps START high: field stays tripped
    outputs["breaker"]["T02"] = 0
    field.apply_outputs(outputs)
    outputs["breaker"]["T02"] = 1
    field.apply_outputs(outputs)
    for _ in range(3):
        snap = field.step()
        assert snap["sensors"]["turbine_state"]["T02"] == "SAFETY_TRIPPED"
        assert snap["sensors"]["breaker_closed"]["T02"] is False
        assert snap["meters"]["turbines"]["T02"]["power_kw"] == 0


def test_local_reset_requires_supervisor_and_cool_sensor_and_does_not_restart(field):
    outputs = all_closed()
    field.apply_outputs(outputs)
    field.step()
    field.inject_fault("T02", "overheat")
    field.step()
    early = field.local_reset("T02", supervisor="sup-a")
    assert early["accepted"] is False  # sensor still hot (90 C) at the trip tick
    with pytest.raises(ValueError):
        field.local_reset("T02", supervisor="")
    field.step()  # stopped turbine cools below the reset setpoint
    accepted = field.local_reset("T02", supervisor="sup-a")
    assert accepted["accepted"] is True
    snap = field.step()
    # START is still high but the trip reset never restarts by itself
    assert snap["sensors"]["safety_latched"]["T02"] is False
    assert snap["sensors"]["turbine_state"]["T02"] == "STOPPED"
    outputs["run"]["T02"] = False
    field.apply_outputs(outputs)
    outputs["run"]["T02"] = True
    outputs["breaker"]["T02"] = 0
    field.apply_outputs(outputs)
    outputs["breaker"]["T02"] = 1
    field.apply_outputs(outputs)
    snap = field.step()
    assert snap["sensors"]["turbine_state"]["T02"] == "RUNNING"


def test_safety_sensor_failure_trips_even_with_healthy_scada_temperature(field):
    field.apply_outputs(all_closed())
    field.step()
    field.inject_fault("T03", "sensor_failure")
    snap = field.step()
    assert snap["sensors"]["safety_reason"]["T03"] == "SENSOR_INVALID"
    assert snap["meters"]["turbines"]["T03"]["bearing_temp_c"] < 85  # SCADA view still looks fine


def test_safety_service_loss_fails_safe():
    safety = SafetyClient()
    try:
        plant = WindFarmField(weather(), safety)
        plant.apply_outputs(all_closed())
        plant.step()
        safety.process.kill()
        safety.process.wait()
        snap = plant.step()
        assert set(snap["sensors"]["turbine_state"].values()) == {"SAFETY_UNAVAILABLE"}
        assert snap["meters"]["export_kw"] == 0
        assert plant.local_reset("T01", supervisor="sup-a")["accepted"] is False
    finally:
        safety.close()


def test_weather_end_holds_last_sample(field):
    field.apply_outputs(all_closed())
    for _ in range(12):
        snap = field.step()
    assert snap["sim_time_utc"] == "2024-01-01T07:00:00Z"
    assert snap["tick"] == 12


# --- processes: UDP field I/O and local console --------------------------------------------

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _udp(port, message, timeout=2.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        return json.loads(sock.recv(65535))


def _console(path, message, timeout=2.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall((json.dumps(message) + "\n").encode())
        return json.loads(sock.makefile().readline())


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    return False


@pytest.fixture
def field_process(tmp_path):
    port = _free_port()
    sock_path = tmp_path / "console.sock"
    audit = tmp_path / "safety_audit.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "scripts.field_plant", "--port", str(port),
         "--console-socket", str(sock_path), "--audit-log", str(audit), "--interval", "0.1",
         "--scenario-faults"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert _wait(lambda: _udp(port, {"op": "read"}, timeout=0.3)["tick"] >= 0)
        yield port, sock_path, audit, proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()


def test_udp_field_io_reads_writes_and_rejects_bad_requests(field_process):
    port, *_ = field_process
    first = _udp(port, {"op": "read"})
    assert set(first) >= {"tick", "sim_time_utc", "sensors", "meters"}
    ack = _udp(port, {"op": "write", "outputs": all_closed()})
    assert ack == {"ok": True}
    assert _wait(lambda: _udp(port, {"op": "read"})["meters"]["export_kw"] > 0)
    assert "error" in _udp(port, {"op": "write", "outputs": {"run": {"T99": True}}})
    assert "error" in _udp(port, {"op": "reset", "turbine_id": "T01"})  # no reset over field I/O
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(b"not json", ("127.0.0.1", port))
        assert "error" in json.loads(sock.recv(65535))


def test_local_console_socket_is_owner_only_and_resets_are_audited(field_process):
    port, sock_path, audit, _ = field_process
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
    _udp(port, {"op": "write", "outputs": all_closed()})
    _udp(port, {"op": "fault", "turbine_id": "T02", "kind": "overheat"})
    assert _wait(lambda: _udp(port, {"op": "read"})["sensors"]["safety_latched"]["T02"])
    assert "error" in _console(sock_path, {"op": "reset", "turbine_id": "T02"})
    assert _wait(lambda: _console(sock_path, {"op": "reset", "turbine_id": "T02",
                                              "supervisor": "sup-a"})["accepted"])
    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    assert entries and all(e["action"] == "LOCAL_RESET" and e["turbine_id"] == "T02" for e in entries)
    assert entries[-1]["accepted"] is True and entries[-1]["supervisor"] == "sup-a"
    assert all(e["peer_uid"] == os.getuid() for e in entries)
