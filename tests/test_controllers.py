"""Level 1 controllers: PLC (run requests, breaker closers) and MU/IED/RTU metering over Modbus TCP."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from scripts.plc import (COIL_BREAKER, COIL_RUN, DI_BREAKER, DI_SAFETY, IR_COMM, IR_STATE,
                         STATE_CODES, plc_scan)
from scripts.rtu import (ALERT_BREAKER_INCONSISTENT, ALERT_LEAKAGE, ALERT_OUT_OF_RANGE,
                         ALERT_OVERCURRENT, HR, ied_alerts, rtu_table)
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")


def snapshot(closed=True, state="RUNNING", latched=False, power=1500.0, feeder_closed=True):
    gen_a = power * 1000 / (3 ** 0.5 * 690 * 0.95) if state == "RUNNING" else 0.0
    out_kw = 3 * power * 0.99 if state == "RUNNING" else 0.0
    return {
        "tick": 5, "sim_time_utc": "2024-01-01T04:00:00Z",
        "sensors": {
            "breaker_closed": {**{t: closed for t in TURBINES}, "FDR": feeder_closed},
            "breaker_trip_reason": {**{t: None for t in TURBINES}, "FDR": None},
            "turbine_state": {t: state for t in TURBINES},
            "safety_latched": {t: latched for t in TURBINES},
            "safety_reason": {t: "BEARING_OVERTEMP" if latched else None for t in TURBINES},
        },
        "meters": {
            "turbines": {t: {"wind_mps": 11.0, "power_kw": power if state == "RUNNING" else 0.0,
                             "gen_kv": 0.69 if state == "RUNNING" else 0.0, "gen_a": gen_a,
                             "downstream_a": gen_a if closed else 0.0, "rpm": 13.0,
                             "bearing_temp_c": 41.5} for t in TURBINES},
            "ambient_c": 20.0,
            "transformer": {"in_kw": out_kw / 0.99, "out_kw": out_kw, "out_kv": 33.0},
            "feeder": {"kv": 33.0, "current_a": out_kw * 1000 / (3 ** 0.5 * 33000 * 0.95) if feeder_closed else 0.0},
            "export_kw": out_kw if feeder_closed else 0.0,
        },
    }


# --- PLC ladder logic (pure) -------------------------------------------------------------

def test_first_scan_synchronises_coils_with_field_contacts():
    coils = [False] * 7
    coils, outputs, di, ir = plc_scan(coils, snapshot(closed=True, state="RUNNING"), first_scan=True)
    assert all(coils[COIL_RUN[t]] for t in TURBINES)
    assert all(coils[COIL_BREAKER[n]] for n in (*TURBINES, "FDR"))
    assert outputs == {"run": {t: True for t in TURBINES},
                       "breaker": {**{t: 1 for t in TURBINES}, "FDR": 1}}
    assert ir[IR_COMM] == 1 and ir[IR_STATE["T01"]] == STATE_CODES["RUNNING"]


def test_first_scan_of_stopped_plant_keeps_everything_off():
    coils, outputs, *_ = plc_scan([True] * 7, snapshot(closed=False, state="STOPPED", feeder_closed=False),
                                  first_scan=True)
    assert coils == [False] * 7
    assert set(outputs["breaker"].values()) == {0}


def test_later_scans_do_not_follow_contacts():
    coils = [False] * 7
    coils[COIL_BREAKER["T01"]] = True  # trip left the coil high, contact open
    coils, outputs, di, _ = plc_scan(coils, snapshot(closed=False, state="DISCONNECTED"), first_scan=False)
    assert coils[COIL_BREAKER["T01"]] is True and outputs["breaker"]["T01"] == 1
    assert di[DI_BREAKER["T01"]] is False


def test_safety_latch_interlock_drops_run_request():
    coils = [True] * 7
    coils, outputs, di, ir = plc_scan(coils, snapshot(latched=True, state="SAFETY_TRIPPED", closed=False),
                                      first_scan=False)
    assert not any(coils[COIL_RUN[t]] for t in TURBINES)
    assert outputs["run"] == {t: False for t in TURBINES}
    assert all(di[DI_SAFETY[t]] for t in TURBINES)
    assert ir[IR_STATE["T02"]] == STATE_CODES["SAFETY_TRIPPED"]


def test_lost_field_link_reports_comm_loss_and_writes_nothing():
    coils, outputs, di, ir = plc_scan([True] * 7, None, first_scan=False)
    assert outputs is None and ir[IR_COMM] == 0 and all(ir[IR_STATE[t]] == 0 for t in TURBINES)
    assert coils == [True] * 7


# --- MU / IED / RTU (pure) ---------------------------------------------------------------

def test_rtu_table_scales_metering_values():
    table = rtu_table(snapshot(), sequence=9)
    assert table[HR["T01"]["power_kw"]] == 1500
    assert table[HR["T01"]["gen_kv_x100"]] == 69
    assert table[HR["T01"]["breaker_closed"]] == 1
    assert table[HR["farm"]["export_kw"]] == round(3 * 1500 * 0.99)
    assert table[HR["farm"]["feeder_kv_x10"]] == 330
    assert table[HR["farm"]["sequence"]] == 9 and table[HR["farm"]["quality"]] == 1
    assert all(0 <= value <= 65535 for value in table)


def test_healthy_running_plant_raises_no_ied_alerts():
    assert ied_alerts(snapshot()) == {"T01": 0, "T02": 0, "T03": 0, "FDR": 0}


def test_ied_detects_leakage_and_breaker_inconsistency():
    leak = snapshot()
    leak["meters"]["turbines"]["T01"]["downstream_a"] -= 200
    assert ied_alerts(leak)["T01"] & ALERT_LEAKAGE
    stuck = snapshot(closed=False, state="DISCONNECTED")
    stuck["meters"]["turbines"]["T02"]["downstream_a"] = 50.0
    assert ied_alerts(stuck)["T02"] & ALERT_BREAKER_INCONSISTENT


def test_ied_flags_overcurrent_and_out_of_range_feeder_voltage():
    hot = snapshot()
    hot["meters"]["turbines"]["T03"]["gen_a"] = 2100.0
    hot["meters"]["turbines"]["T03"]["downstream_a"] = 2100.0
    assert ied_alerts(hot)["T03"] & ALERT_OVERCURRENT
    high = snapshot()
    high["meters"]["feeder"]["kv"] = 100.0
    assert ied_alerts(high)["FDR"] & ALERT_OUT_OF_RANGE


# --- processes ---------------------------------------------------------------------------

def _port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, ValueError, AttributeError, KeyError, AssertionError):
            pass
        time.sleep(0.05)
    return False


def _udp(port, message):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        return json.loads(sock.recv(65535))


class Stack:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.field_port, self.plc_port, self.rtu_port = _port(socket.SOCK_DGRAM), _port(), _port()
        self.procs = {}

    def start(self, name, *args):
        modules = {"field": "scripts.field_plant", "plc": "scripts.plc", "rtu": "scripts.rtu"}
        log = (self.tmp / f"{name}.log").open("ab")
        self.procs[name] = subprocess.Popen([sys.executable, "-u", "-m", modules[name], *args],
                                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        log.close()

    def start_field(self):
        self.start("field", "--port", str(self.field_port), "--interval", "0.1", "--scenario-faults",
                   "--console-socket", str(self.tmp / "c.sock"), "--audit-log", str(self.tmp / "a.jsonl"))
        assert _wait(lambda: "tick" in _udp(self.field_port, {"op": "read"}))

    def start_plc(self):
        self.start("plc", "--port", str(self.plc_port), "--field-port", str(self.field_port), "--scan", "0.05")
        assert _wait(lambda: self.plc().read_input_registers(0, count=8, device_id=1).registers[IR_COMM] == 1)

    def start_rtu(self, *extra):
        self.start("rtu", "--port", str(self.rtu_port), "--field-port", str(self.field_port), "--scan", "0.05", *extra)
        assert _wait(lambda: self.rtu_read()[HR["farm"]["quality"]] == 1)

    def stop(self, name):
        proc = self.procs.pop(name)
        proc.terminate()
        proc.wait(timeout=10)

    def plc(self):
        client = ModbusTcpClient("127.0.0.1", port=self.plc_port, timeout=2)
        assert client.connect()
        return client

    def rtu_read(self):
        client = ModbusTcpClient("127.0.0.1", port=self.rtu_port, timeout=2)
        assert client.connect()
        try:
            return client.read_holding_registers(0, count=50, device_id=1).registers
        finally:
            client.close()

    def field(self):
        return _udp(self.field_port, {"op": "read"})

    def close(self):
        for name in list(self.procs):
            self.stop(name)


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    try:
        s.start_field()
        yield s
    finally:
        s.close()


def _energise(client):
    assert not client.write_coils(0, [True] * 7, device_id=1).isError()


def test_plc_drives_field_and_rtu_reports_matching_export(stack):
    stack.start_plc()
    stack.start_rtu()
    client = stack.plc()
    try:
        assert client.read_coils(0, count=7, device_id=1).bits[:7] == [False] * 7
        _energise(client)
        assert _wait(lambda: stack.field()["meters"]["export_kw"] > 0)
        assert _wait(lambda: all(client.read_discrete_inputs(0, count=7, device_id=1).bits[:4]))
        assert _wait(lambda: client.read_input_registers(0, count=8, device_id=1).registers[IR_STATE["T01"]]
                     == STATE_CODES["RUNNING"])
        assert _wait(lambda: stack.rtu_read()[HR["farm"]["export_kw"]] > 0)
        field_kw = stack.field()["meters"]["export_kw"]
        assert abs(stack.rtu_read()[HR["farm"]["export_kw"]] - field_kw) <= 0.05 * field_kw + 1
    finally:
        client.close()


def test_plc_rejects_writes_outside_the_coil_map(stack):
    stack.start_plc()
    client = stack.plc()
    try:
        assert client.write_coil(7, True, device_id=1).isError()
        assert client.write_register(0, 1, device_id=1).isError()
    finally:
        client.close()


def test_safety_trip_interlock_holds_run_request_low(stack):
    stack.start_plc()
    client = stack.plc()
    try:
        _energise(client)
        assert _wait(lambda: stack.field()["sensors"]["turbine_state"]["T02"] == "RUNNING")
        _udp(stack.field_port, {"op": "fault", "turbine_id": "T02", "kind": "overheat"})
        assert _wait(lambda: client.read_discrete_inputs(0, count=7, device_id=1).bits[DI_SAFETY["T02"]])
        assert _wait(lambda: client.read_coils(0, count=7, device_id=1).bits[COIL_RUN["T02"]] is False)
        client.write_coil(COIL_RUN["T02"], True, device_id=1)  # START while latched
        time.sleep(0.4)
        assert client.read_coils(0, count=7, device_id=1).bits[COIL_RUN["T02"]] is False
        assert stack.field()["sensors"]["turbine_state"]["T02"] == "SAFETY_TRIPPED"
    finally:
        client.close()


def test_plc_restart_does_not_open_closed_breakers(stack):
    stack.start_plc()
    client = stack.plc()
    _energise(client)
    client.close()
    assert _wait(lambda: stack.field()["sensors"]["turbine_state"]["T01"] == "RUNNING")
    stack.stop("plc")
    stack.start_plc()
    time.sleep(0.4)
    snap = stack.field()
    assert all(snap["sensors"]["breaker_closed"].values())
    assert set(snap["sensors"]["turbine_state"].values()) == {"RUNNING"}
    client = stack.plc()
    try:
        assert client.read_coils(0, count=7, device_id=1).bits[:7] == [True] * 7
    finally:
        client.close()


def test_overcurrent_trip_shows_coil_contact_mismatch(stack):
    stack.start_plc()
    client = stack.plc()
    try:
        _energise(client)
        assert _wait(lambda: stack.field()["sensors"]["turbine_state"]["T01"] == "RUNNING")
        _udp(stack.field_port, {"op": "fault", "turbine_id": "T01", "kind": "overcurrent"})
        assert _wait(lambda: not client.read_discrete_inputs(0, count=7, device_id=1).bits[DI_BREAKER["T01"]])
        assert client.read_coils(0, count=7, device_id=1).bits[COIL_BREAKER["T01"]] is True
    finally:
        client.close()


def test_rtu_data_table_accepts_false_data_by_default(stack):
    stack.start_rtu("--scan", "1.0")
    client = ModbusTcpClient("127.0.0.1", port=stack.rtu_port, timeout=2)
    assert client.connect()
    try:
        register = HR["farm"]["feeder_kv_x10"]
        assert not client.write_register(register, 1000, device_id=1).isError()
        assert client.read_holding_registers(register, count=1, device_id=1).registers[0] == 1000
        assert _wait(lambda: stack.rtu_read()[register] == 330)  # next refresh overwrites it
    finally:
        client.close()


def test_hardened_rtu_rejects_writes(stack):
    stack.start_rtu("--reject-writes")
    client = ModbusTcpClient("127.0.0.1", port=stack.rtu_port, timeout=2)
    assert client.connect()
    try:
        assert client.write_register(HR["farm"]["feeder_kv_x10"], 1000, device_id=1).isError()
    finally:
        client.close()


def test_field_link_loss_is_reported_by_plc_and_rtu(stack):
    stack.start_plc()
    stack.start_rtu()
    stack.stop("field")
    client = stack.plc()
    try:
        assert _wait(lambda: client.read_input_registers(0, count=8, device_id=1).registers[IR_COMM] == 0)
        assert _wait(lambda: stack.rtu_read()[HR["farm"]["quality"]] == 0)
    finally:
        client.close()
