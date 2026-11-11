"""Level 2 alarm engine: rules over collected PLC/RTU data, debounce, lifecycle, false-data cross-checks."""
import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from scripts.alarm_engine import AlarmEngine, AlarmStore, evaluate
from scripts.plant_collector import PlantHistorian, poll_once
from scripts.plc import IR_COMM
from scripts.rtu import HR, TABLE_SIZE
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")
NOW = 1_000_000.0


def plc_values(state="RUNNING", cmd=True, closed=True, latched=False):
    return {"run_request": {t: True for t in TURBINES},
            "breaker_command": {n: cmd for n in (*TURBINES, "FDR")},
            "breaker_closed": {n: closed for n in (*TURBINES, "FDR")},
            "safety_latched": {t: latched for t in TURBINES},
            "turbine_state": {t: state for t in TURBINES},
            "field_link": True, "scan": 1, "field_tick": 1}


def rtu_values(power=1500, closed=True, export=None):
    running = power > 0
    total = 3 * power
    return {"turbines": {t: {"power_kw": power, "gen_kv": 0.69 if running else 0.0, "gen_a": 1321 if running else 0,
                             "downstream_a": 1321 if running and closed else 0, "rpm": 13.0 if running else 0.0,
                             "bearing_temp_c": 41.0, "wind_mps": 11.0, "breaker_closed": closed, "alerts": 0}
                         for t in TURBINES},
            "farm": {"transformer_in_kw": total, "transformer_out_kw": round(total * 0.99),
                     "feeder_kv": 33.0, "feeder_a": 82.0,
                     "export_kw": round(total * 0.99) if export is None else export,
                     "feeder_closed": closed, "alerts": 0},
            "sequence": 1, "field_link": True}


def latest(plc=None, rtu=None, plc_quality="GOOD", rtu_quality="GOOD"):
    return {"plc": {"quality": plc_quality, "values": plc if plc_quality == "GOOD" else None},
            "rtu": {"quality": rtu_quality, "values": rtu if rtu_quality == "GOOD" else None}}


def keys(snapshot, heartbeat=NOW):
    conditions, _ = evaluate(snapshot, heartbeat, NOW)
    return set(conditions)


# --- rules (pure) ------------------------------------------------------------------------

def test_healthy_running_plant_has_no_alarms():
    assert keys(latest(plc_values(), rtu_values())) == set()


def test_healthy_stopped_plant_has_no_alarms():
    assert keys(latest(plc_values(state="STOPPED", cmd=False, closed=False), rtu_values(power=0, closed=False))) == set()


def test_breaker_trip_is_a_command_position_mismatch():
    plc = plc_values()
    plc["breaker_closed"]["T01"] = False
    plc["turbine_state"]["T01"] = "DISCONNECTED"
    rtu = rtu_values()
    rtu["turbines"]["T01"].update(power_kw=0, breaker_closed=False, gen_kv=0.0, gen_a=0, downstream_a=0)
    rtu["farm"]["export_kw"] = round(2 * 1500 * 0.99)
    conditions, _ = evaluate(latest(plc, rtu), NOW, NOW)
    assert set(conditions) == {"BREAKER:CB-T01"}
    assert conditions["BREAKER:CB-T01"]["priority"] == "HIGH"
    plc = plc_values(cmd=False)
    assert "BREAKER:CB-FDR" in keys(latest(plc, rtu_values()))  # commanded open, still closed


def test_safety_trip_is_critical_and_hides_its_own_breaker_mismatch():
    plc = plc_values()
    plc["safety_latched"]["T02"] = True
    plc["turbine_state"]["T02"] = "SAFETY_TRIPPED"
    plc["breaker_closed"]["T02"] = False
    rtu = rtu_values()
    rtu["turbines"]["T02"].update(power_kw=0, breaker_closed=False, gen_kv=0.0, gen_a=0, downstream_a=0)
    rtu["farm"]["export_kw"] = round(2 * 1500 * 0.99)
    conditions, _ = evaluate(latest(plc, rtu), NOW, NOW)
    assert set(conditions) == {"SAFETY:T02"}
    assert conditions["SAFETY:T02"]["priority"] == "CRITICAL"


def test_safety_system_unavailable_is_critical():
    plc = plc_values(state="SAFETY_UNAVAILABLE", closed=False, latched=True)
    conditions, _ = evaluate(latest(plc, rtu_values(power=0, closed=False)), NOW, NOW)
    assert {k for k in conditions if k.startswith("SAFETY_UNAVAILABLE")} == {f"SAFETY_UNAVAILABLE:{t}" for t in TURBINES}


def test_bad_controller_data_raises_only_comm_and_holds_dependent_alarms():
    conditions, held = evaluate(latest(plc_values(), None, rtu_quality="COMM_LOSS"), NOW, NOW)
    assert set(conditions) == {"COMM:rtu"}
    assert "Communication" in conditions["COMM:rtu"]["message"] or "COMM_LOSS" in conditions["COMM:rtu"]["message"]
    assert any("ENERGY_BALANCE".startswith(p) for p in held) and any("IED:T01:LEAKAGE".startswith(p) for p in held)
    assert not any("BREAKER:CB-T01".startswith(p) for p in held)


def test_stopped_collector_raises_collector_stale_and_holds_everything():
    conditions, held = evaluate(latest(plc_values(), rtu_values()), NOW - 30, NOW, stale_after=10)
    assert set(conditions) == {"COLLECTOR_STALE"}
    assert any("BREAKER:CB-T01".startswith(p) for p in held)
    assert set(evaluate(latest(plc_values(), rtu_values()), None, NOW)[0]) == {"COLLECTOR_STALE"}


def test_rtu_ied_bits_become_alarms():
    rtu = rtu_values()
    rtu["turbines"]["T03"]["alerts"] = 1 | 4
    assert {"IED:T03:LEAKAGE", "IED:T03:OVERCURRENT"} <= keys(latest(plc_values(), rtu))


# --- false-data detection ----------------------------------------------------------------

def test_forged_breaker_position_in_rtu_disagrees_with_plc():
    rtu = rtu_values()
    rtu["turbines"]["T02"]["breaker_closed"] = False
    assert "CROSSCHECK:BREAKER:CB-T02" in keys(latest(plc_values(), rtu))


def test_forged_turbine_power_disagrees_with_plc_state():
    plc = plc_values()
    plc["turbine_state"]["T03"] = "STOPPED"
    found = keys(latest(plc, rtu_values()))
    assert "CROSSCHECK:POWER:T03" in found


def test_forged_export_breaks_energy_balance():
    assert "ENERGY_BALANCE" in keys(latest(plc_values(), rtu_values(export=0)))
    assert "ENERGY_BALANCE" not in keys(latest(plc_values(), rtu_values(export=round(4500 * 0.99) - 20)))


def test_physically_impossible_values_are_range_alarms():
    rtu = rtu_values()
    rtu["farm"]["feeder_kv"] = 100.0
    rtu["turbines"]["T01"]["bearing_temp_c"] = 900.0
    assert {"RANGE:feeder_kv", "RANGE:T01:bearing_temp_c"} <= keys(latest(plc_values(), rtu))


# --- lifecycle ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    yield AlarmEngine(store, off_delay=2), store
    store.close()


def cond(key="BREAKER:CB-T01", on_delay=3, priority="HIGH"):
    return {key: {"priority": priority, "message": "breaker mismatch", "on_delay": on_delay}}


def test_debounce_needs_consecutive_cycles(engine):
    eng, store = engine
    eng.step(cond(), set(), "t1")
    eng.step(cond(), set(), "t2")
    eng.step({}, set(), "t3")          # condition gone: debounce restarts
    eng.step(cond(), set(), "t4")
    eng.step(cond(), set(), "t5")
    assert store.open_alarms() == []
    eng.step(cond(), set(), "t6")
    alarm = store.open_alarms()[0]
    assert alarm["key"] == "BREAKER:CB-T01" and alarm["active"] and not alarm["acked"]
    assert alarm["raised_at"] == "t6"


def test_ack_then_clear_closes_the_alarm(engine):
    eng, store = engine
    eng.step(cond(on_delay=1), set(), "t1")
    alarm_id = store.open_alarms()[0]["id"]
    assert store.acknowledge(alarm_id, "olivia", "t2") is True
    assert store.acknowledge(alarm_id, "olivia", "t2") is False  # already acknowledged
    eng.step({}, set(), "t3")
    assert store.open_alarms()[0]["active"]  # off-delay not yet met
    eng.step({}, set(), "t4")
    assert store.open_alarms() == []
    kinds = [e["event"] for e in store.journal()]
    assert kinds == ["CLEARED", "ACKED", "RAISED"]
    assert store.journal()[1]["actor"] == "olivia"


def test_cleared_unacknowledged_alarm_stays_listed_until_acked(engine):
    eng, store = engine
    eng.step(cond(on_delay=1), set(), "t1")
    eng.step({}, set(), "t2")
    eng.step({}, set(), "t3")
    alarm = store.open_alarms()[0]
    assert alarm["active"] is False and alarm["acked"] is False and alarm["cleared_at"] == "t3"
    store.acknowledge(alarm["id"], "olivia", "t4")
    assert store.open_alarms() == []


def test_returning_condition_reactivates_the_open_alarm_and_needs_new_ack(engine):
    eng, store = engine
    eng.step(cond(on_delay=1), set(), "t1")
    first = store.open_alarms()[0]["id"]
    eng.step({}, set(), "t2")
    eng.step({}, set(), "t3")                      # cleared but never acknowledged: still open
    eng.step(cond(on_delay=1), set(), "t4")
    alarm = store.open_alarms()[0]
    assert alarm["id"] == first and alarm["active"] and alarm["acked"] is False


def test_recurrence_after_ack_and_clear_is_a_new_alarm(engine):
    eng, store = engine
    eng.step(cond(on_delay=1), set(), "t1")
    first = store.open_alarms()[0]["id"]
    store.acknowledge(first, "olivia", "t2")
    eng.step({}, set(), "t3")
    eng.step({}, set(), "t4")
    eng.step(cond(on_delay=1), set(), "t5")
    assert store.open_alarms()[0]["id"] != first


def test_held_alarms_do_not_clear_while_data_is_bad(engine):
    eng, store = engine
    eng.step(cond(on_delay=1), set(), "t1")
    for stamp in ("t2", "t3", "t4"):
        eng.step({}, {"BREAKER:"}, stamp)
    assert store.open_alarms()[0]["active"] is True


def test_alarms_sorted_by_priority(engine):
    eng, store = engine
    eng.step({**cond("IED:T01:LEAKAGE", 1, "MEDIUM"), **cond("SAFETY:T02", 1, "CRITICAL"),
              **cond("BREAKER:CB-T01", 1, "HIGH")}, set(), "t1")
    assert [a["priority"] for a in store.open_alarms()] == ["CRITICAL", "HIGH", "MEDIUM"]


def test_engine_restart_keeps_active_alarms(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    AlarmEngine(store).step(cond(on_delay=1), set(), "t1")
    store.close()
    store = AlarmStore(tmp_path / "a.sqlite")
    engine = AlarmEngine(store, off_delay=1)
    engine.step(cond(on_delay=1), set(), "t2")
    assert len(store.open_alarms()) == 1  # same instance, not a duplicate
    engine.step({}, set(), "t3")
    assert store.open_alarms()[0]["active"] is False
    store.close()


# --- live stack --------------------------------------------------------------------------

def _port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(predicate, timeout=25.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, ValueError, AttributeError, KeyError, AssertionError, TypeError):
            pass
        time.sleep(interval)
    return False


def _udp(port, message):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        return json.loads(sock.recv(65535))


class Live:
    def __init__(self, tmp_path, rtu_scan="0.05"):
        self.tmp = tmp_path
        self.field_port, self.plc_port, self.rtu_port = _port(socket.SOCK_DGRAM), _port(), _port()
        self.procs = []
        weather = tmp_path / "weather.csv"
        weather.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n" + "".join(
            f"2024-01-{1 + h // 24:02d}T{h % 24:02d}:00:00Z,10.0,20.0\n" for h in range(72)))
        self._start("scripts.field_plant", "--port", str(self.field_port), "--interval", "0.5",
                    "--weather", str(weather), "--scenario-faults",
                    "--console-socket", str(tmp_path / "c.sock"), "--audit-log", str(tmp_path / "s.jsonl"))
        assert _wait(lambda: "tick" in _udp(self.field_port, {"op": "read"}))
        self._start("scripts.plc", "--port", str(self.plc_port), "--field-port", str(self.field_port), "--scan", "0.05")
        self._start("scripts.rtu", "--port", str(self.rtu_port), "--field-port", str(self.field_port),
                    "--scan", rtu_scan)
        assert _wait(lambda: self.modbus(self.plc_port).read_input_registers(0, count=8, device_id=1)
                     .registers[IR_COMM] == 1)
        assert _wait(lambda: self.rtu_table()[HR["farm"]["quality"]] == 1)
        self.historian = PlantHistorian(tmp_path / "plant.sqlite")
        self.store = AlarmStore(tmp_path / "alarms.sqlite")
        self.engine = AlarmEngine(self.store)

    def _start(self, module, *args):
        with (self.tmp / f"{module}.log").open("ab") as log:
            self.procs.append(subprocess.Popen([sys.executable, "-u", "-m", module, *args], cwd=ROOT,
                                               stdout=log, stderr=subprocess.STDOUT))

    def modbus(self, port):
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=2)
        assert client.connect()
        return client

    def rtu_table(self):
        client = self.modbus(self.rtu_port)
        try:
            return client.read_holding_registers(0, count=TABLE_SIZE, device_id=1).registers
        finally:
            client.close()

    def energise(self):
        client = self.modbus(self.plc_port)
        assert not client.write_coils(0, [True] * 7, device_id=1).isError()
        client.close()
        assert _wait(lambda: set(_udp(self.field_port, {"op": "read"})["sensors"]["turbine_state"].values())
                     == {"RUNNING"})

    def cycle(self):
        asyncio.run(poll_once(("127.0.0.1", self.plc_port), ("127.0.0.1", self.rtu_port), self.historian,
                              max_age_seconds=15))
        now = time.time()
        conditions, held = evaluate(self.historian.latest(), self.historian.last_heartbeat(), now)
        self.engine.step(conditions, held, "now")
        return {a["key"] for a in self.store.open_alarms() if a["active"]}

    def close(self):
        self.historian.close()
        self.store.close()
        for proc in self.procs:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture
def live(tmp_path):
    stack = Live(tmp_path)
    try:
        yield stack
    finally:
        stack.close()


@pytest.fixture
def live_slow_rtu(tmp_path):
    stack = Live(tmp_path, rtu_scan="30")
    try:
        yield stack
    finally:
        stack.close()


def test_live_healthy_plant_is_quiet(live):
    live.energise()
    for _ in range(8):
        active = live.cycle()
        time.sleep(0.1)
    assert active == set()


def test_live_overcurrent_trip_raises_breaker_alarm(live):
    live.energise()
    _udp(live.field_port, {"op": "fault", "turbine_id": "T01", "kind": "overcurrent"})
    assert _wait(lambda: "BREAKER:CB-T01" in live.cycle(), interval=0.1)


def test_live_rtu_outage_raises_comm_alarm(live):
    live.energise()
    live.cycle()
    live.procs[2].terminate()
    live.procs[2].wait(timeout=10)
    assert _wait(lambda: "COMM:rtu" in live.cycle(), interval=0.1)


def test_live_false_data_injection_is_detected(live_slow_rtu):
    """Forge the RTU table like an attacker: export 0 kW and a 100 kV feeder, with a new sequence."""
    live = live_slow_rtu
    client = live.modbus(live.plc_port)
    assert not client.write_coils(0, [True] * 7, device_id=1).isError()
    client.close()
    assert _wait(lambda: set(_udp(live.field_port, {"op": "read"})["sensors"]["turbine_state"].values())
                 == {"RUNNING"})
    # the slow RTU refreshes only every 30 s; restart it so its table reflects the running plant
    live.procs[2].terminate()
    live.procs[2].wait(timeout=10)
    live._start("scripts.rtu", "--port", str(live.rtu_port), "--field-port", str(live.field_port), "--scan", "30")
    assert _wait(lambda: live.rtu_table()[HR["farm"]["export_kw"]] > 0)
    live.cycle()
    table = live.rtu_table()
    farm = HR["farm"]
    client = live.modbus(live.rtu_port)
    assert not client.write_register(farm["export_kw"], 0, device_id=1).isError()
    assert not client.write_register(farm["feeder_kv_x10"], 1000, device_id=1).isError()
    assert not client.write_register(farm["sequence"], (table[farm["sequence"]] + 1) % 65536, device_id=1).isError()
    client.close()
    assert _wait(lambda: {"ENERGY_BALANCE", "RANGE:feeder_kv"} <= live.cycle(), interval=0.1)


def test_engine_cli_flags_missing_collector(tmp_path):
    PlantHistorian(tmp_path / "plant.sqlite").close()
    proc = subprocess.Popen([sys.executable, "-u", "-m", "scripts.alarm_engine",
                             "--plant-db", str(tmp_path / "plant.sqlite"), "--db", str(tmp_path / "alarms.sqlite"),
                             "--interval", "0.1"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "ALARMS_READY" in proc.stdout.readline()

        def raised():
            with sqlite3.connect(tmp_path / "alarms.sqlite") as conn:
                return conn.execute("SELECT key FROM alarms WHERE active=1").fetchall() == [("COLLECTOR_STALE",)]

        assert _wait(raised)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()
