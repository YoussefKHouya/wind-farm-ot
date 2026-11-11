"""Level 2 operator HMI for the simulated plant: overview, alarms with acknowledgement, commands, trend."""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient
from pymodbus.client import ModbusTcpClient

from scripts.alarm_engine import AlarmEngine, AlarmStore
from scripts.command_service import hash_password
from scripts.plant_collector import PlantHistorian, decode_plc, decode_rtu
from scripts.plant_hmi import create_app
from scripts.plc import IR_COMM
from scripts.rtu import HR, TABLE_SIZE
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")
OP, SUP = ("olivia", "op-pass-1"), ("sam", "sup-pass-1")


def _port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(predicate, timeout=25.0):
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


def plc_frame(scan):
    ir = [1, 1, 1, 1, scan, 40, 0, 0]
    return [True] * 7, [True, True, True, True, False, False, False], ir


def rtu_frame(sequence, power=1500):
    hr = [0] * TABLE_SIZE
    for t in TURBINES:
        hr[HR[t]["power_kw"]] = power
        hr[HR[t]["gen_kv_x100"]] = 69
        hr[HR[t]["breaker_closed"]] = 1
    farm = HR["farm"]
    hr[farm["export_kw"]] = round(3 * power * 0.99)
    hr[farm["feeder_kv_x10"]] = 330
    hr[farm["feeder_closed"]] = 1
    hr[farm["sequence"]] = sequence
    hr[farm["quality"]] = 1
    return hr


def seed(tmp_path, samples=5, heartbeat=True):
    plant = PlantHistorian(tmp_path / "plant.sqlite")
    for i in range(1, samples + 1):
        stamp = f"2026-01-01T00:00:{i:02d}Z"
        plant.record("plc", decode_plc(*plc_frame(i)), stamp, max_age_seconds=60)
        plant.record("rtu", decode_rtu(rtu_frame(i, power=1000 + 100 * i)), stamp, max_age_seconds=60)
    if heartbeat:
        plant.heartbeat(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    plant.close()
    store = AlarmStore(tmp_path / "alarms.sqlite")
    engine = AlarmEngine(store)
    engine.step({"SAFETY:T02": {"priority": "CRITICAL", "message": "T02 safety trip latched", "on_delay": 1},
                 "IED:T01:LEAKAGE": {"priority": "MEDIUM", "message": "leak", "on_delay": 1}}, set(), "t1")
    store.close()


def app_for(tmp_path, commands_url="http://127.0.0.1:9"):
    return TestClient(create_app(plant_db=tmp_path / "plant.sqlite", alarm_db=tmp_path / "alarms.sqlite",
                                 commands_url=commands_url))


# --- read-only views ---------------------------------------------------------------------

def test_page_has_operator_screen_sections_and_keeps_credentials_out_of_storage(tmp_path):
    seed(tmp_path)
    page = app_for(tmp_path).get("/")
    assert page.status_code == 200
    text = page.text
    for marker in ('id="single-line"', 'id="alarm-list"', 'id="trend"', 'id="faceplate"', 'id="login"',
                   'id="quality"'):
        assert marker in text
    assert "SIMULATED PLANT" in text
    assert "localStorage" not in text and "sessionStorage" not in text and "document.cookie" not in text


def test_overview_reports_values_quality_collector_and_sorted_alarms(tmp_path):
    seed(tmp_path)
    data = app_for(tmp_path).get("/api/overview").json()
    assert data["mode"] == "simulated-plant-operations"
    assert data["plc"]["quality"] == "GOOD" and data["rtu"]["quality"] == "GOOD"
    assert data["rtu"]["values"]["turbines"]["T01"]["power_kw"] == 1500
    assert data["collector"]["ok"] is True and data["collector"]["heartbeat_age_s"] < 60
    assert [a["key"] for a in data["alarms"]] == ["SAFETY:T02", "IED:T01:LEAKAGE"]


def test_overview_without_data_is_explicit_not_zero(tmp_path):
    data = app_for(tmp_path).get("/api/overview").json()
    assert data["plc"] == {"quality": "NO_DATA", "observed_at_utc": None, "values": None}
    assert data["collector"]["ok"] is False and data["collector"]["heartbeat_age_s"] is None
    assert data["alarms"] == []


def test_trend_is_chronological(tmp_path):
    seed(tmp_path, samples=6)
    points = app_for(tmp_path).get("/api/trend?points=4").json()["points"]
    assert [p["t"] for p in points] == [f"2026-01-01T00:00:{i:02d}Z" for i in (3, 4, 5, 6)]
    assert points[-1]["export_kw"] == round(3 * 1600 * 0.99)
    assert points[-1]["turbines"] == {"T01": 1600, "T02": 1600, "T03": 1600}


def test_journal_lists_alarm_events(tmp_path):
    seed(tmp_path)
    events = app_for(tmp_path).get("/api/journal").json()["events"]
    assert {e["event"] for e in events} == {"RAISED"}


# --- control path without a command service ----------------------------------------------

def test_ack_without_credentials_is_refused_before_any_lookup(tmp_path):
    seed(tmp_path)
    assert app_for(tmp_path).post("/api/alarms/1/ack").status_code == 401


def test_control_with_command_service_down_is_503(tmp_path):
    seed(tmp_path)
    client = app_for(tmp_path)
    assert client.post("/api/commands", json={"action": "STOP", "target": "T01"}, auth=OP).status_code == 503
    assert client.post("/api/alarms/1/ack", auth=OP).status_code == 503
    store = AlarmStore(tmp_path / "alarms.sqlite")
    assert not any(a["acked"] for a in store.open_alarms())
    store.close()


# --- full path: HMI -> command service -> PLC -> field ----------------------------------

class Stack:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.field_port, self.plc_port, self.cmd_port = _port(socket.SOCK_DGRAM), _port(), _port()
        self.procs = []
        weather = tmp_path / "weather.csv"
        weather.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n" + "".join(
            f"2024-01-{1 + h // 24:02d}T{h % 24:02d}:00:00Z,10.0,20.0\n" for h in range(72)))
        users = tmp_path / "users.json"
        users.write_text(json.dumps({"users": {
            "olivia": {"role": "operator", **hash_password(OP[1], n=2 ** 10)},
            "sam": {"role": "supervisor", **hash_password(SUP[1], n=2 ** 10)}}}))
        os.chmod(users, 0o600)
        self._start("scripts.field_plant", "--port", str(self.field_port), "--interval", "0.5",
                    "--weather", str(weather), "--console-socket", str(tmp_path / "c.sock"),
                    "--audit-log", str(tmp_path / "s.jsonl"))
        assert _wait(lambda: "tick" in _udp(self.field_port, {"op": "read"}))
        self._start("scripts.plc", "--port", str(self.plc_port), "--field-port", str(self.field_port), "--scan", "0.05")
        assert _wait(lambda: self._plc_ok())
        self._start("scripts.command_service", "serve", "--port", str(self.cmd_port),
                    "--plc", f"127.0.0.1:{self.plc_port}", "--db", str(tmp_path / "commands.sqlite"),
                    "--users", str(users), "--pulse", "0.3")
        self.commands_url = f"http://127.0.0.1:{self.cmd_port}"
        assert _wait(lambda: self._whoami() == 200)

    def _plc_ok(self):
        client = ModbusTcpClient("127.0.0.1", port=self.plc_port, timeout=2)
        assert client.connect()
        try:
            return client.read_input_registers(0, count=8, device_id=1).registers[IR_COMM] == 1
        finally:
            client.close()

    def _whoami(self):
        request = urllib.request.Request(self.commands_url + "/api/whoami")
        request.add_header("Authorization", "Basic " + base64.b64encode(b"olivia:op-pass-1").decode())
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status

    def _start(self, module, *args):
        with (self.tmp / f"{module}.log").open("ab") as log:
            self.procs.append(subprocess.Popen([sys.executable, "-u", "-m", module, *args], cwd=ROOT,
                                               stdout=log, stderr=subprocess.STDOUT))

    def field(self):
        return _udp(self.field_port, {"op": "read"})

    def close(self):
        for proc in self.procs:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    try:
        seed(tmp_path)
        yield s, app_for(tmp_path, s.commands_url)
    finally:
        s.close()


def test_command_through_hmi_reaches_the_field_and_is_audited_as_the_user(stack):
    s, hmi = stack
    response = hmi.post("/api/commands", json={"action": "CLOSE", "target": "CB-FDR"}, auth=OP)
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert _wait(lambda: s.field()["sensors"]["breaker_closed"]["FDR"] is True)
    audit = hmi.get("/api/audit", auth=SUP).json()["entries"]
    assert audit[0]["actor"] == "olivia" and audit[0]["action"] == "CLOSE"


def test_refusals_and_bad_logins_pass_through_unchanged(stack):
    _, hmi = stack
    assert hmi.post("/api/commands", json={"action": "STOP", "target": "T01"},
                    auth=("olivia", "nope")).status_code == 401
    refused = hmi.post("/api/commands", json={"action": "LOCAL_RESET", "target": "T01"}, auth=SUP)
    assert refused.status_code == 400 and refused.json()["reason"] == "LOCAL_CONSOLE_ONLY"
    assert hmi.post("/api/locks", json={"target": "T02", "locked": True}, auth=OP).status_code == 403
    assert hmi.post("/api/locks", json={"target": "T02", "locked": True}, auth=SUP).status_code == 200
    assert "T02" in hmi.get("/api/locks", auth=OP).json()["locks"]
    assert hmi.get("/api/whoami", auth=SUP).json() == {"user": "sam", "role": "supervisor"}


def test_alarm_ack_is_attributed_to_the_authenticated_user(stack, tmp_path):
    _, hmi = stack
    alarm_id = hmi.get("/api/overview").json()["alarms"][0]["id"]
    assert hmi.post(f"/api/alarms/{alarm_id}/ack", auth=("olivia", "wrong")).status_code == 401
    response = hmi.post(f"/api/alarms/{alarm_id}/ack", auth=OP)
    assert response.status_code == 200 and response.json()["acked"] is True
    assert hmi.post(f"/api/alarms/{alarm_id}/ack", auth=OP).status_code == 409
    assert hmi.post("/api/alarms/9999/ack", auth=OP).status_code == 409
    acked = [e for e in hmi.get("/api/journal").json()["events"] if e["event"] == "ACKED"]
    assert len(acked) == 1 and acked[0]["actor"] == "olivia" and acked[0]["alarm_id"] == alarm_id
