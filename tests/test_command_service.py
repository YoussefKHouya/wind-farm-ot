"""Level 2 command service: authenticated operator commands, interlocks, maintenance locks, audit."""
import json
import os
import socket
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from pymodbus.client import ModbusTcpClient

from scripts.command_service import create_app, hash_password, verify_password
from scripts.plc import COIL_BREAKER, COIL_RUN, DI_BREAKER, DI_SAFETY, IR_COMM
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")
FAST_HASH = {"n": 2 ** 10}


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


def _console(path, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        sock.sendall((json.dumps(message) + "\n").encode())
        return json.loads(sock.makefile().readline())


def write_users(path, users):
    entries = {name: {"role": role, **hash_password(password, **FAST_HASH)} for name, (role, password) in users.items()}
    path.write_text(json.dumps({"users": entries}))
    os.chmod(path, 0o600)


class Plant:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.field_port, self.plc_port = _port(socket.SOCK_DGRAM), _port()
        self.console = tmp_path / "console.sock"
        self.procs = []
        weather = tmp_path / "weather.csv"
        rows = [f"2024-01-01T{h % 24:02d}:00:00Z" if h < 24 else f"2024-01-02T{h - 24:02d}:00:00Z"
                for h in range(48)]
        weather.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n"
                           + "".join(f"{stamp},10.0,20.0\n" for stamp in rows))
        self._start("scripts.field_plant", "--port", str(self.field_port), "--interval", "0.2",
                    "--weather", str(weather), "--scenario-faults",
                    "--console-socket", str(self.console), "--audit-log", str(tmp_path / "safety.jsonl"))
        assert _wait(lambda: "tick" in _udp(self.field_port, {"op": "read"}))
        self._start("scripts.plc", "--port", str(self.plc_port), "--field-port", str(self.field_port),
                    "--scan", "0.05")
        assert _wait(lambda: self.plc_inputs()[2][IR_COMM] == 1)

    def _start(self, module, *args):
        with (self.tmp / f"{module}.log").open("ab") as log:
            self.procs.append(subprocess.Popen([sys.executable, "-u", "-m", module, *args], cwd=ROOT,
                                               stdout=log, stderr=subprocess.STDOUT))

    def plc_inputs(self):
        client = ModbusTcpClient("127.0.0.1", port=self.plc_port, timeout=2)
        assert client.connect()
        try:
            return (client.read_coils(0, count=7, device_id=1).bits[:7],
                    client.read_discrete_inputs(0, count=7, device_id=1).bits[:7],
                    client.read_input_registers(0, count=8, device_id=1).registers)
        finally:
            client.close()

    def field(self):
        return _udp(self.field_port, {"op": "read"})

    def state(self, turbine):
        return self.field()["sensors"]["turbine_state"][turbine]

    def close(self):
        for proc in self.procs:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture
def plant(tmp_path):
    p = Plant(tmp_path)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def users(tmp_path):
    path = tmp_path / "users.json"
    write_users(path, {"olivia": ("operator", "op-pass-1"), "sam": ("supervisor", "sup-pass-1")})
    return path


@pytest.fixture
def api(plant, users, tmp_path):
    app = create_app(db_path=tmp_path / "commands.sqlite", users_path=users,
                     plc=("127.0.0.1", plant.plc_port), pulse_seconds=0.3)
    return TestClient(app)


OP, SUP = ("olivia", "op-pass-1"), ("sam", "sup-pass-1")


def command(api, auth, action, target):
    return api.post("/api/commands", json={"action": action, "target": target}, auth=auth)


def energise(api, plant):
    for name in ("CB-T01", "CB-T02", "CB-T03", "CB-FDR"):
        assert command(api, OP, "CLOSE", name).json()["accepted"] is True
    for t in TURBINES:
        assert command(api, OP, "START", t).json()["accepted"] is True
    assert _wait(lambda: set(plant.field()["sensors"]["turbine_state"].values()) == {"RUNNING"})


def audit(api):
    return api.get("/api/audit", auth=SUP).json()["entries"]


# --- passwords ---------------------------------------------------------------------------

def test_password_hash_is_salted_and_verifies():
    first, second = hash_password("secret-1", **FAST_HASH), hash_password("secret-1", **FAST_HASH)
    assert first["salt"] != second["salt"] and first["hash"] != second["hash"]
    assert "secret-1" not in json.dumps(first)
    assert verify_password("secret-1", first) is True
    assert verify_password("secret-2", first) is False


# --- authentication and roles ------------------------------------------------------------

def test_missing_or_wrong_credentials_are_refused_and_audited(api):
    assert api.post("/api/commands", json={"action": "STOP", "target": "T01"}).status_code == 401
    assert command(api, ("olivia", "wrong"), "STOP", "T01").status_code == 401
    assert command(api, ("mallory", "x"), "STOP", "T01").status_code == 401
    entries = audit(api)
    failures = [e for e in entries if e["reason"] == "AUTH_FAILED"]
    assert len(failures) == 3 and all(e["accepted"] is False for e in failures)
    assert {e["actor"] for e in failures} == {None, "olivia", "mallory"}
    assert all(e["client"] for e in failures)


def test_operator_controls_the_plant(api, plant):
    energise(api, plant)
    response = command(api, OP, "STOP", "T03")
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert _wait(lambda: plant.state("T03") == "STOPPED")
    assert command(api, OP, "OPEN", "CB-T01").json()["accepted"] is True
    assert _wait(lambda: plant.state("T01") == "DISCONNECTED")
    entry = audit(api)[0]
    assert entry["actor"] == "olivia" and entry["role"] == "operator" and entry["action"] == "OPEN"
    assert entry["target"] == "CB-T01" and entry["writes"] == [{"coil": COIL_BREAKER["T01"], "value": False}]


def test_operator_cannot_set_maintenance_locks(api):
    response = api.post("/api/locks", json={"target": "T02", "locked": True}, auth=OP)
    assert response.status_code == 403
    assert audit(api)[0]["reason"] == "ROLE_NOT_PERMITTED"


def test_invalid_commands_are_rejected_and_audited(api):
    for body in ({"action": "START", "target": "CB-T01"}, {"action": "OPEN", "target": "T01"},
                 {"action": "EXPLODE", "target": "T01"}, {"action": "START"}, ["START"]):
        assert api.post("/api/commands", json=body, auth=OP).status_code == 400
    assert api.post("/api/commands", content=b"not json", auth=OP,
                    headers={"content-type": "application/json"}).status_code == 400
    assert sum(e["reason"] == "INVALID_COMMAND" for e in audit(api)) == 6


def test_local_reset_is_never_available_remotely(api):
    response = command(api, SUP, "LOCAL_RESET", "T02")
    assert response.status_code == 400 and response.json()["reason"] == "LOCAL_CONSOLE_ONLY"
    assert audit(api)[0]["reason"] == "LOCAL_CONSOLE_ONLY"


# --- interlocks --------------------------------------------------------------------------

def test_maintenance_lock_blocks_start_and_close_but_never_stop_or_open(api, plant):
    energise(api, plant)
    assert api.post("/api/locks", json={"target": "T02", "locked": True, "note": "gearbox"},
                    auth=SUP).json()["locked"] is True
    assert api.get("/api/locks", auth=OP).json()["locks"]["T02"]["by"] == "sam"
    assert command(api, OP, "STOP", "T02").json()["accepted"] is True
    assert command(api, OP, "OPEN", "CB-T02").json()["accepted"] is True
    refused = command(api, OP, "START", "T02")
    assert refused.status_code == 409 and refused.json()["reason"] == "MAINTENANCE_LOCK"
    assert command(api, SUP, "CLOSE", "CB-T02").json()["reason"] == "MAINTENANCE_LOCK"
    assert command(api, OP, "START", "T01").json()["accepted"] is True  # other turbines unaffected
    api.post("/api/locks", json={"target": "T02", "locked": False}, auth=SUP)
    assert command(api, OP, "CLOSE", "CB-T02").json()["accepted"] is True
    assert command(api, OP, "START", "T02").json()["accepted"] is True
    assert _wait(lambda: plant.state("T02") == "RUNNING")


def test_safety_trip_blocks_restart_until_local_reset(api, plant):
    energise(api, plant)
    _udp(plant.field_port, {"op": "fault", "turbine_id": "T02", "kind": "overheat"})
    assert _wait(lambda: plant.plc_inputs()[1][DI_SAFETY["T02"]] is True)
    refused = command(api, OP, "START", "T02")
    assert refused.status_code == 409 and refused.json()["reason"] == "SAFETY_TRIP_LATCHED"
    assert command(api, OP, "CLOSE", "CB-T02").json()["reason"] == "SAFETY_TRIP_LATCHED"
    assert _wait(lambda: _console(plant.console, {"op": "reset", "turbine_id": "T02",
                                                  "supervisor": "sam"})["accepted"])
    assert _wait(lambda: plant.plc_inputs()[1][DI_SAFETY["T02"]] is False)
    assert _wait(lambda: plant.state("T02") == "STOPPED")
    time.sleep(0.6)  # three plant ticks: a trip reset never restarts the turbine by itself
    assert plant.state("T02") == "STOPPED"
    assert command(api, OP, "CLOSE", "CB-T02").json()["accepted"] is True  # fresh closer pulse
    assert command(api, OP, "START", "T02").json()["accepted"] is True
    assert _wait(lambda: plant.state("T02") == "RUNNING")


def test_close_after_overcurrent_trip_sends_a_fresh_pulse(api, plant):
    energise(api, plant)
    _udp(plant.field_port, {"op": "fault", "turbine_id": "T01", "kind": "overcurrent"})
    assert _wait(lambda: plant.plc_inputs()[1][DI_BREAKER["T01"]] is False)
    assert plant.plc_inputs()[0][COIL_BREAKER["T01"]] is True  # closer coil still high
    response = command(api, OP, "CLOSE", "CB-T01")
    assert response.json()["accepted"] is True
    coil = COIL_BREAKER["T01"]
    assert response.json()["writes"] == [{"coil": coil, "value": False}, {"coil": coil, "value": True}]
    assert _wait(lambda: plant.field()["sensors"]["breaker_closed"]["T01"] is True)


def test_start_issues_a_run_request_on_the_plc(api, plant):
    assert command(api, OP, "START", "T03").json()["accepted"] is True
    assert plant.plc_inputs()[0][COIL_RUN["T03"]] is True


# --- availability and persistence --------------------------------------------------------

def test_unreachable_plc_is_refused_and_audited(users, tmp_path):
    api = TestClient(create_app(db_path=tmp_path / "c.sqlite", users_path=users,
                                plc=("127.0.0.1", _port()), pulse_seconds=0.1))
    response = command(api, OP, "STOP", "T01")
    assert response.status_code == 503 and response.json()["reason"] == "PLC_UNAVAILABLE"
    assert audit(api)[0]["reason"] == "PLC_UNAVAILABLE"


def test_audit_survives_restart(plant, users, tmp_path):
    db = tmp_path / "persist.sqlite"
    first = TestClient(create_app(db_path=db, users_path=users, plc=("127.0.0.1", plant.plc_port)))
    command(first, OP, "STOP", "T01")
    second = TestClient(create_app(db_path=db, users_path=users, plc=("127.0.0.1", plant.plc_port)))
    assert audit(second)[-1]["action"] == "STOP"


# --- CLI ---------------------------------------------------------------------------------

def test_add_user_cli_stores_only_a_hash_with_owner_only_permissions(tmp_path):
    path = tmp_path / "users.json"
    result = subprocess.run([sys.executable, "-m", "scripts.command_service", "add-user", "nora",
                             "--role", "operator", "--users", str(path), "--password-stdin"],
                            cwd=ROOT, input="correct-horse-9\n", capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    stored = json.loads(path.read_text())["users"]["nora"]
    assert stored["role"] == "operator" and "correct-horse-9" not in path.read_text()
    assert verify_password("correct-horse-9", stored)
    bad = subprocess.run([sys.executable, "-m", "scripts.command_service", "add-user", "x", "--role", "admin",
                          "--users", str(path), "--password-stdin"], cwd=ROOT, input="pw-long-enough\n",
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2
