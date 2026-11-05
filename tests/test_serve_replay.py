"""Foreground replay runner: real child processes on ephemeral loopback ports."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from scripts import hmi, ot_gateway, scada_collector, serve_replay
from scripts.ot_protocol import WORDS, decode_registers
from scripts.scada_collector import Historian
from scripts.simulate import ROOT

STARTUP_DEADLINE = 60.0
STOP_DEADLINE = 30.0
FAST = ("--replay-interval", "0.2", "--poll-interval", "0.05", "--max-age", "5")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def squat(port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    return sock


def port_free(port):
    """True only if nothing answers and the address can be bound again."""
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    return True


def pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def eventually(predicate, timeout=STOP_DEADLINE, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Runner:
    def __init__(self, tmp_path, *extra, modbus_port=None, hmi_port=None):
        self.modbus_port = modbus_port or free_port()
        self.hmi_port = hmi_port or free_port()
        self.db = tmp_path / "historian.sqlite"
        self.log = tmp_path / "runner.log"
        with self.log.open("wb") as out:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "scripts.serve_replay",
                 "--db", str(self.db), "--modbus-port", str(self.modbus_port),
                 "--hmi-port", str(self.hmi_port), *FAST, *extra],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)

    def text(self):
        return self.log.read_text(encoding="utf-8", errors="replace")

    def pids(self):
        return {name: int(pid) for name, pid in re.findall(r"started (\w+) pid=(\d+)", self.text())}

    def wait_for(self, needle):
        deadline = time.monotonic() + STARTUP_DEADLINE
        while time.monotonic() < deadline:
            if needle in self.text():
                return
            if self.proc.poll() is not None:
                pytest.fail(f"runner exited {self.proc.returncode} before {needle!r}:\n{self.text()}")
            time.sleep(0.05)
        pytest.fail(f"no {needle!r} within {STARTUP_DEADLINE}s:\n{self.text()}")

    def wait_ready(self):
        self.wait_for("REPLAY_READY")

    def wait_exit(self, timeout=STARTUP_DEADLINE):
        return self.proc.wait(timeout=timeout)

    def assert_nothing_left(self):
        pids = self.pids()
        assert all(eventually(lambda p=pid: pid_gone(p)) for pid in pids.values()), self.text()
        assert port_free(self.modbus_port) and port_free(self.hmi_port), self.text()

    def reap(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=STOP_DEADLINE)
        for pid in self.pids().values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def launch(tmp_path):
    started = []

    def _launch(*extra, **ports):
        runner = Runner(tmp_path, *extra, **ports)
        started.append(runner)
        return runner

    yield _launch
    for runner in started:
        runner.reap()


def hmi_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def defaults_of(main, monkeypatch):
    """Capture the real parser of a service main() without running the service."""
    class Captured(Exception):
        pass

    def capture(self, *args, **kwargs):
        raise Captured(self)

    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Captured) as caught:
            main()
    return caught.value.args[0]


def test_cli_defaults_match_each_service(monkeypatch):
    gateway = defaults_of(ot_gateway.main, monkeypatch)
    collector = defaults_of(scada_collector.main, monkeypatch)
    hmi_parser = defaults_of(hmi.main, monkeypatch)
    args = serve_replay.parse_args([])
    assert args.source == gateway.get_default("source")
    assert args.replay_interval == gateway.get_default("interval")
    assert args.modbus_port == gateway.get_default("port") == collector.get_default("port")
    assert args.db == collector.get_default("db") == hmi_parser.get_default("db")
    assert args.logs_source == collector.get_default("logs_source")
    assert args.poll_interval == collector.get_default("interval")
    assert args.max_age == collector.get_default("max_age")
    assert args.hmi_port == hmi_parser.get_default("port")


@pytest.mark.parametrize("argv", [
    ["--replay-interval", "30"],                       # default max-age 25 cannot cover a tick
    ["--max-age", "10"],                               # equal to replay interval
    ["--max-age", "0.4", "--replay-interval", "0.1"],  # below default poll interval
    ["--poll-interval", "0.5", "--replay-interval", "0.1", "--max-age", "0.5"],
    ["--poll-interval", "0"],
    ["--replay-interval", "-1"],
    ["--replay-interval", "nan"],
    ["--max-age", "inf"],
    ["--modbus-port", "9000", "--hmi-port", "9000"],
    ["--modbus-port", "0"],
    ["--hmi-port", "65536"],
    ["--startup-timeout", "0"],
    ["--stop-timeout", "0"],
])
def test_invalid_configuration_is_rejected_before_any_child_starts(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        serve_replay.parse_args(argv)
    assert caught.value.code == 2


def test_child_commands_use_this_interpreter_and_loopback_read_only_modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = serve_replay.parse_args(["--db", "rel/h.sqlite", "--source", "s.csv", "--logs-source", "l.csv",
                                    "--modbus-port", "1502", "--hmi-port", "1808"])
    commands = serve_replay.child_commands(args)
    assert list(commands) == ["gateway", "collector", "hmi"]
    for name, module in (("gateway", "scripts.ot_gateway"), ("collector", "scripts.scada_collector"),
                         ("hmi", "scripts.hmi")):
        assert commands[name][0] == sys.executable
        assert commands[name][commands[name].index("-m") + 1] == module
    gateway, collector, viewer = commands.values()
    for command in (gateway, collector):
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert command[command.index("--port") + 1] == "1502"
    assert viewer[viewer.index("--port") + 1] == "1808"
    # children run from the repository root, so relative paths must be anchored here
    assert gateway[gateway.index("--source") + 1] == str(tmp_path / "s.csv")
    assert collector[collector.index("--logs-source") + 1] == str(tmp_path / "l.csv")
    assert collector[collector.index("--db") + 1] == viewer[viewer.index("--db") + 1] == str(tmp_path / "rel/h.sqlite")


def test_exit_status_never_reports_success_for_a_dead_child():
    assert serve_replay.exit_status(3) == 3
    assert serve_replay.exit_status(0) == 1
    assert serve_replay.exit_status(-signal.SIGKILL) == 128 + signal.SIGKILL


def test_stop_children_kills_after_bounded_wait_and_stops_newest_first():
    code = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('armed', flush=True); time.sleep(120)")
    stubborn = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    polite = subprocess.Popen([sys.executable, "-c", "import time; print('armed', flush=True); time.sleep(120)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert stubborn.stdout.readline().strip() == polite.stdout.readline().strip() == "armed"
        started = time.monotonic()
        serve_replay.stop_children([serve_replay.Child("polite", polite), serve_replay.Child("stubborn", stubborn)],
                                   grace=0.3)
        assert time.monotonic() - started < 10
        assert stubborn.returncode == -signal.SIGKILL
        assert polite.returncode == -signal.SIGTERM
    finally:
        for proc in (stubborn, polite):
            proc.kill()
            proc.wait()
            proc.stdout.close()


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_end_to_end_replay_then_clean_shutdown(launch, stop_signal):
    sources = (ot_gateway.DEFAULT_SOURCE, scada_collector.LOGS_DEFAULT)
    before = [sha256(path) for path in sources]
    runner = launch()
    runner.wait_ready()
    assert set(runner.pids()) == {"gateway", "collector", "hmi"}

    client = ModbusTcpClient("127.0.0.1", port=runner.modbus_port, timeout=2)
    assert client.connect()
    response = client.read_input_registers(0, count=WORDS, device_id=1)
    client.close()
    assert not response.isError() and decode_registers(response.registers)["sequence"] >= 1

    status, body = hmi_get(runner.hmi_port, "/api/fleet")
    fleet = json.loads(body)
    assert status == 200 and fleet["mode"] == "historical-recorded-scada-replay"
    assert set(fleet["turbines"]) == set(ot_gateway.IDS.values())

    def observations():
        return json.loads(hmi_get(runner.hmi_port, "/api/history/WT02")[1])["observations"]

    assert eventually(lambda: len(observations()) >= 2)
    seen = observations()[-1]

    runner.proc.send_signal(stop_signal)
    assert runner.wait_exit(STOP_DEADLINE) == 0, runner.text()
    runner.assert_nothing_left()

    historian = Historian(runner.db)
    try:
        for turbine in ot_gateway.IDS.values():
            assert historian.history(turbine), turbine
        assert seen in historian.history("WT02", limit=100000)
        with scada_collector.LOGS_DEFAULT.open(newline="", encoding="utf-8") as handle:
            expected_events = sum(1 for _ in csv.DictReader(handle))
        stored = historian.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        assert stored == expected_events
        assert historian.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # the collector stops before the gateway, so shutdown is not recorded as an outage
        assert [event for event in historian.events() if event["kind"] == "COMM_LOSS"] == []
    finally:
        historian.close()
    assert [sha256(path) for path in sources] == before


def test_occupied_modbus_port_fails_even_though_gateway_prints_ready_and_stays_alive(launch):
    port = free_port()
    with squat(port) as squatter:
        runner = launch("--startup-timeout", "4", modbus_port=port)
        code = runner.wait_exit()
        assert code != 0, runner.text()
        assert "REPLAY_READY" not in runner.text()
        pids = runner.pids()
        assert set(pids) == {"gateway"}, runner.text()
        assert eventually(lambda: pid_gone(pids["gateway"]))
        assert squatter.fileno() != -1
        assert port_free(runner.hmi_port)
    assert port_free(port)


def test_occupied_hmi_port_stops_gateway_and_collector_already_running(launch):
    port = free_port()
    with squat(port):
        runner = launch(hmi_port=port)
        code = runner.wait_exit()
        assert code == 1, runner.text()  # uvicorn's own bind-failure status
        assert "REPLAY_READY" not in runner.text()
        pids = runner.pids()
        assert set(pids) == {"gateway", "collector", "hmi"}, runner.text()
        assert all(eventually(lambda p=pid: pid_gone(p)) for pid in pids.values())
        assert port_free(runner.modbus_port)
    assert port_free(port)


def test_collector_startup_failure_propagates_status_and_stops_gateway(launch, tmp_path):
    runner = launch("--logs-source", str(tmp_path / "missing.csv"))
    assert runner.wait_exit() == 1, runner.text()
    assert "REPLAY_READY" not in runner.text()
    assert set(runner.pids()) == {"gateway", "collector"}
    runner.assert_nothing_left()


def test_gateway_startup_failure_on_bad_source_starts_nothing_else(launch, tmp_path):
    runner = launch("--source", str(tmp_path / "missing.csv"))
    assert runner.wait_exit() == 1, runner.text()
    assert set(runner.pids()) == {"gateway"}
    runner.assert_nothing_left()


@pytest.mark.parametrize("victim", ["gateway", "collector", "hmi"])
def test_child_dying_after_ready_stops_the_rest_with_nonzero_status(launch, victim):
    runner = launch()
    runner.wait_ready()
    os.kill(runner.pids()[victim], signal.SIGKILL)
    assert runner.wait_exit(STOP_DEADLINE) == 128 + signal.SIGKILL, runner.text()
    assert victim in runner.text().split("REPLAY_READY")[-1]
    runner.assert_nothing_left()


def test_signal_during_startup_still_stops_children(launch):
    runner = launch()
    runner.wait_for("started gateway")
    runner.proc.send_signal(signal.SIGTERM)
    assert runner.wait_exit(STOP_DEADLINE) == 0, runner.text()
    runner.assert_nothing_left()


def test_children_do_not_outlive_a_killed_runner(launch):
    runner = launch()
    runner.wait_ready()
    runner.proc.kill()
    runner.proc.wait(timeout=STOP_DEADLINE)
    runner.assert_nothing_left()


def test_restart_on_existing_historian_needs_fresh_observations(launch):
    ports = {"modbus_port": free_port(), "hmi_port": free_port()}
    first = launch(**ports)
    first.wait_ready()
    first.proc.send_signal(signal.SIGTERM)
    assert first.wait_exit(STOP_DEADLINE) == 0, first.text()
    first.assert_nothing_left()

    second = launch(**ports)
    second.wait_ready()
    second.proc.send_signal(signal.SIGINT)
    assert second.wait_exit(STOP_DEADLINE) == 0, second.text()
    second.assert_nothing_left()


def test_foreign_working_gateway_on_the_port_is_not_mistaken_for_ours(launch):
    port = free_port()
    foreign = subprocess.Popen([sys.executable, "-u", "-m", "scripts.ot_gateway", "--port", str(port)],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert "MODBUS_READY" in foreign.stdout.readline()
        assert eventually(lambda: not port_free_to_connect(port))
        runner = launch("--startup-timeout", "4", modbus_port=port)
        assert runner.wait_exit() == 1, runner.text()
        assert "REPLAY_READY" not in runner.text()
        assert set(runner.pids()) == {"gateway"}, runner.text()
        assert eventually(lambda: pid_gone(runner.pids()["gateway"]))
        assert foreign.poll() is None  # the runner only touches its own children
    finally:
        foreign.terminate()
        foreign.wait(timeout=STOP_DEADLINE)
        foreign.stdout.close()


def port_free_to_connect(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_historian_rows_from_an_earlier_run_do_not_count_as_ready(tmp_path):
    db_path = tmp_path / "h.sqlite"
    assert serve_replay.historian_fresh(db_path, datetime(2026, 1, 1, tzinfo=timezone.utc)) is False
    db = Historian(db_path)
    for device_id in ot_gateway.IDS:
        db.record(device_id, {"sequence": 1, "quality": "GOOD"}, "2026-01-01T00:00:10Z")
    db.close()
    assert serve_replay.historian_fresh(db_path, datetime(2026, 1, 1, tzinfo=timezone.utc)) is True
    assert serve_replay.historian_fresh(db_path, datetime(2026, 1, 2, tzinfo=timezone.utc)) is False


def test_each_child_is_fully_stopped_before_the_older_one_is_signalled():
    calls = []

    class Recorder:
        pid = 0

        def __init__(self, name):
            self.name = name

        def terminate(self):
            calls.append(("terminate", self.name))

        def wait(self, timeout=None):
            calls.append(("wait", self.name))

    serve_replay.stop_children([serve_replay.Child(name, Recorder(name)) for name in ("gateway", "collector", "hmi")],
                               grace=1)
    assert calls == [("terminate", "hmi"), ("wait", "hmi"), ("terminate", "collector"), ("wait", "collector"),
                     ("terminate", "gateway"), ("wait", "gateway")]
