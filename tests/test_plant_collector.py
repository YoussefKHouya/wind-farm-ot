"""Level 2 plant SCADA collector: polls PLC and RTU over Modbus TCP into the plant historian."""
import asyncio
import json
import socket
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from scripts.plant_collector import (PlantHistorian, decode_plc, decode_rtu, parse_endpoint,
                                     poll_once)
from scripts.plc import COIL_BREAKER, COIL_RUN, DI_BREAKER, DI_SAFETY, IR_COMM, IR_SCAN, IR_STATE, IR_TICK
from scripts.rtu import HR, TABLE_SIZE
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")
NOW = "2026-01-01T00:00:00Z"


def at(seconds):
    return f"2026-01-01T00:00:{seconds:02d}Z"


def plc_frame(scan=1, state=1, comm=1, closed=True, latched=False):
    coils = [True] * 7
    di = [False] * 7
    for name, index in DI_BREAKER.items():
        di[index] = closed
    for t, index in DI_SAFETY.items():
        di[index] = latched
    ir = [0] * 8
    for t, index in IR_STATE.items():
        ir[index] = state
    ir[IR_COMM], ir[IR_SCAN], ir[IR_TICK] = comm, scan, 40
    return coils, di, ir


def rtu_frame(sequence=1, quality=1, power=1500):
    hr = [0] * TABLE_SIZE
    for t in TURBINES:
        hr[HR[t]["power_kw"]] = power
        hr[HR[t]["gen_kv_x100"]] = 69
        hr[HR[t]["gen_a"]] = 1321
        hr[HR[t]["downstream_a"]] = 1321
        hr[HR[t]["rpm_x10"]] = 130
        hr[HR[t]["bearing_temp_x10"]] = (-55) & 0xFFFF  # -5.5 C exercises signed decoding
        hr[HR[t]["wind_x10"]] = 112
        hr[HR[t]["breaker_closed"]] = 1
    farm = HR["farm"]
    hr[farm["transformer_in_kw"]] = 3 * power
    hr[farm["transformer_out_kw"]] = round(3 * power * 0.99)
    hr[farm["feeder_kv_x10"]] = 330
    hr[farm["feeder_a_x10"]] = 829
    hr[farm["export_kw"]] = round(3 * power * 0.99)
    hr[farm["feeder_closed"]] = 1
    hr[farm["sequence"]] = sequence
    hr[farm["quality"]] = quality
    return hr


# --- decoding ----------------------------------------------------------------------------

def test_decode_plc_maps_coils_contacts_and_states():
    values = decode_plc(*plc_frame())
    assert values["run_request"] == {t: True for t in TURBINES}
    assert values["breaker_command"] == {n: True for n in (*TURBINES, "FDR")}
    assert values["breaker_closed"] == {n: True for n in (*TURBINES, "FDR")}
    assert values["safety_latched"] == {t: False for t in TURBINES}
    assert values["turbine_state"] == {t: "RUNNING" for t in TURBINES}
    assert values["field_link"] is True and values["scan"] == 1 and values["field_tick"] == 40


def test_decode_plc_rejects_unknown_state_code():
    with pytest.raises(ValueError, match="state"):
        decode_plc(*plc_frame(state=42))


def test_decode_rtu_scales_and_signs_values():
    values = decode_rtu(rtu_frame())
    t01 = values["turbines"]["T01"]
    assert t01["power_kw"] == 1500 and t01["gen_kv"] == 0.69 and t01["rpm"] == 13.0
    assert t01["bearing_temp_c"] == -5.5 and t01["wind_mps"] == 11.2 and t01["breaker_closed"] is True
    assert values["farm"]["feeder_kv"] == 33.0 and values["farm"]["feeder_a"] == 82.9
    assert values["farm"]["export_kw"] == round(4500 * 0.99)
    assert values["sequence"] == 1 and values["field_link"] is True


@pytest.mark.parametrize("field,value", [("breaker_closed", 7), ("alerts", 99)])
def test_decode_rtu_rejects_values_outside_the_map(field, value):
    hr = rtu_frame()
    hr[HR["T02"][field]] = value
    with pytest.raises(ValueError):
        decode_rtu(hr)


def test_decode_rejects_wrong_frame_length():
    with pytest.raises(ValueError):
        decode_rtu([0] * 10)
    with pytest.raises(ValueError):
        decode_plc([True] * 7, [False] * 7, [0] * 3)


def test_parse_endpoint():
    assert parse_endpoint("10.10.1.10:502") == ("10.10.1.10", 502)
    with pytest.raises(ValueError):
        parse_endpoint("10.10.1.10")
    with pytest.raises(ValueError):
        parse_endpoint("host:99999")


# --- historian quality -------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    historian = PlantHistorian(tmp_path / "plant.sqlite")
    yield historian
    historian.close()


def test_good_samples_are_stored_with_poll_time(db):
    db.record("rtu", decode_rtu(rtu_frame(sequence=1)), at(0))
    db.record("rtu", decode_rtu(rtu_frame(sequence=2)), at(1))
    current = db.latest()["rtu"]
    assert current["quality"] == "GOOD" and current["observed_at_utc"] == at(1)
    assert current["values"]["sequence"] == 2
    assert [s["values"]["sequence"] for s in db.history("rtu")] == [2, 1]


def test_repeated_sequence_is_not_a_new_sample_and_turns_stale(db):
    sample = decode_rtu(rtu_frame(sequence=5))
    assert db.record("rtu", sample, at(0), max_age_seconds=3) is True
    assert db.record("rtu", sample, at(2), max_age_seconds=3) is False
    assert db.latest()["rtu"]["quality"] == "GOOD"
    db.record("rtu", sample, at(4), max_age_seconds=3)
    current = db.latest()["rtu"]
    assert current["quality"] == "STALE" and current["values"] is None
    assert len(db.history("rtu")) == 1 and db.history("rtu")[0]["values"]["sequence"] == 5
    db.record("rtu", sample, at(5), max_age_seconds=3)
    assert db.latest()["rtu"]["quality"] == "STALE"  # the same frame never recovers
    db.record("rtu", decode_rtu(rtu_frame(sequence=6)), at(6), max_age_seconds=3)
    assert db.latest()["rtu"]["quality"] == "GOOD"
    assert [e["kind"] for e in db.events()] == ["RECOVERED", "STALE"]


def test_unavailable_marks_null_values_once_and_keeps_history(db):
    db.record("plc", decode_plc(*plc_frame(scan=1)), at(0))
    db.mark("plc", "COMM_LOSS", at(1))
    db.mark("plc", "COMM_LOSS", at(2))
    current = db.latest()["plc"]
    assert current["quality"] == "COMM_LOSS" and current["values"] is None
    assert len(db.history("plc")) == 1
    assert [e["kind"] for e in db.events()] == ["COMM_LOSS"]
    with pytest.raises(ValueError):
        db.mark("plc", "FINE", at(3))


def test_device_reported_field_loss_is_its_own_quality(db):
    db.record("rtu", decode_rtu(rtu_frame(sequence=1)), at(0))
    db.record("rtu", decode_rtu(rtu_frame(sequence=1, quality=0)), at(1))
    assert db.latest()["rtu"]["quality"] == "FIELD_LINK_LOST"
    db.record("plc", decode_plc(*plc_frame(scan=1)), at(0))
    db.record("plc", decode_plc(*plc_frame(scan=2, comm=0, state=0)), at(1))
    assert db.latest()["plc"]["quality"] == "FIELD_LINK_LOST"
    assert db.latest()["plc"]["values"] is None


def test_unknown_device_rejected(db):
    with pytest.raises(ValueError):
        db.mark("hmi", "COMM_LOSS", NOW)


# --- live stack --------------------------------------------------------------------------

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
        except (OSError, ValueError, AttributeError, KeyError, AssertionError, TypeError):
            pass
        time.sleep(0.05)
    return False


def _udp(port, message):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        return json.loads(sock.recv(65535))


class Plant:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.field_port, self.plc_port, self.rtu_port = _port(socket.SOCK_DGRAM), _port(), _port()
        self.procs = {}

    def start(self, name, module, *args):
        with (self.tmp / f"{name}.log").open("ab") as log:
            self.procs[name] = subprocess.Popen([sys.executable, "-u", "-m", module, *args], cwd=ROOT,
                                                stdout=log, stderr=subprocess.STDOUT)

    def up(self):
        self.start("field", "scripts.field_plant", "--port", str(self.field_port), "--interval", "0.5",
                   "--console-socket", str(self.tmp / "c.sock"), "--audit-log", str(self.tmp / "a.jsonl"))
        assert _wait(lambda: "tick" in _udp(self.field_port, {"op": "read"}))
        self.start("plc", "scripts.plc", "--port", str(self.plc_port), "--field-port", str(self.field_port),
                   "--scan", "0.05")
        self.start("rtu", "scripts.rtu", "--port", str(self.rtu_port), "--field-port", str(self.field_port),
                   "--scan", "0.05")
        assert _wait(lambda: self.client(self.plc_port).read_input_registers(0, count=8, device_id=1)
                     .registers[IR_COMM] == 1)
        assert _wait(lambda: self.client(self.rtu_port).read_holding_registers(0, count=TABLE_SIZE, device_id=1)
                     .registers[HR["farm"]["quality"]] == 1)

    def client(self, port):
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=2)
        assert client.connect()
        return client

    def stop(self, name):
        proc = self.procs.pop(name)
        proc.terminate()
        proc.wait(timeout=10)

    def close(self):
        for name in list(self.procs):
            self.stop(name)

    def poll(self, db):
        asyncio.run(poll_once(("127.0.0.1", self.plc_port), ("127.0.0.1", self.rtu_port), db, max_age_seconds=5))


@pytest.fixture
def plant(tmp_path):
    p = Plant(tmp_path)
    try:
        p.up()
        yield p
    finally:
        p.close()


def test_collector_records_matching_plc_and_rtu_values(plant, db):
    client = plant.client(plant.plc_port)
    assert not client.write_coils(0, [True] * 7, device_id=1).isError()
    client.close()
    assert _wait(lambda: _udp(plant.field_port, {"op": "read"})["meters"]["export_kw"] > 0)

    def running():
        plant.poll(db)
        latest = db.latest()
        return (latest["plc"]["quality"] == "GOOD" and latest["rtu"]["quality"] == "GOOD"
                and latest["rtu"]["values"]["farm"]["export_kw"] > 0
                and set(latest["plc"]["values"]["turbine_state"].values()) == {"RUNNING"})

    assert _wait(running)
    latest = db.latest()
    field = _udp(plant.field_port, {"op": "read"})
    assert latest["plc"]["values"]["breaker_closed"] == field["sensors"]["breaker_closed"]

    def export_tracks_field():
        plant.poll(db)
        field_kw = _udp(plant.field_port, {"op": "read"})["meters"]["export_kw"]
        return abs(db.latest()["rtu"]["values"]["farm"]["export_kw"] - field_kw) <= 0.05 * field_kw + 1

    assert _wait(export_tracks_field)


def test_controller_outage_is_comm_loss_only_for_that_controller(plant, db):
    assert _wait(lambda: (plant.poll(db), db.latest()["rtu"]["quality"] == "GOOD")[1])
    plant.stop("rtu")
    plant.poll(db)
    latest = db.latest()
    assert latest["rtu"]["quality"] == "COMM_LOSS" and latest["rtu"]["values"] is None
    assert latest["plc"]["quality"] == "GOOD"


def test_field_process_loss_is_reported_through_both_controllers(plant, db):
    plant.stop("field")

    def lost():
        plant.poll(db)
        latest = db.latest()
        return latest["plc"]["quality"] == "FIELD_LINK_LOST" and latest["rtu"]["quality"] == "FIELD_LINK_LOST"

    assert _wait(lost)


def test_invalid_rtu_value_is_bad_data(plant, db):
    plant.stop("rtu")
    plant.start("rtu", "scripts.rtu", "--port", str(plant.rtu_port), "--field-port", str(plant.field_port),
                "--scan", "5")
    assert _wait(lambda: plant.client(plant.rtu_port).read_holding_registers(0, count=TABLE_SIZE, device_id=1)
                 .registers[HR["farm"]["quality"]] == 1)
    client = plant.client(plant.rtu_port)
    assert not client.write_register(HR["T01"]["breaker_closed"], 7, device_id=1).isError()
    client.close()
    plant.poll(db)
    assert db.latest()["rtu"]["quality"] == "BAD_DATA"
    assert db.events()[0]["kind"] == "BAD_DATA"


def test_collector_cli_runs_and_writes_historian(plant, tmp_path):
    path = tmp_path / "cli.sqlite"
    proc = subprocess.Popen([sys.executable, "-u", "-m", "scripts.plant_collector",
                             "--plc", f"127.0.0.1:{plant.plc_port}", "--rtu", f"127.0.0.1:{plant.rtu_port}",
                             "--db", str(path), "--interval", "0.1"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "COLLECTOR_READY" in proc.stdout.readline()

        def stored():
            historian = PlantHistorian(path)
            try:
                return {k: v["quality"] for k, v in historian.latest().items()} == {"plc": "GOOD", "rtu": "GOOD"}
            finally:
                historian.close()

        assert _wait(stored)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()


def test_cli_rejects_max_age_not_above_interval():
    result = subprocess.run([sys.executable, "-m", "scripts.plant_collector", "--interval", "2", "--max-age", "2"],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
