"""Foreground launcher for the read-only historical replay stack (Linux, loopback).

Starts the Modbus gateway, collector and HMI as child processes, stays in the
foreground, and stops them all on SIGINT/SIGTERM/SIGHUP, startup failure or the
unexpected exit of any child. Adds no endpoints and writes nothing itself.

A child is only trusted once it provably serves: the gateway prints MODBUS_READY
and stays alive even when its bind failed, and a bare TCP connect would reach
whoever already holds the port, so readiness requires that the child's own
process owns the listener and answers correctly.
"""
import argparse
import contextlib
import ctypes
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from typing import NamedTuple

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from scripts.ot_gateway import DEFAULT_SOURCE, IDS
from scripts.ot_protocol import WORDS, decode_registers
from scripts.scada_collector import DB_DEFAULT, DEFAULT_MAX_AGE_SECONDS, LOGS_DEFAULT
from scripts.simulate import ROOT

HOST = "127.0.0.1"
MODBUS_PORT_DEFAULT = 15020
HMI_PORT_DEFAULT = 18080
REPLAY_INTERVAL_DEFAULT = 10.0
POLL_INTERVAL_DEFAULT = 0.5
PROBE_INTERVAL = 0.1
MONITOR_INTERVAL = 0.25
PROBE_TIMEOUT = 1.0
HMI_MODE = "historical-recorded-scada-replay"
TCP_LISTEN = "0A"
PR_SET_PDEATHSIG = 1
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

_LIBC = ctypes.CDLL(None)  # loaded here because preexec_fn runs between fork and exec


class Child(NamedTuple):
    name: str
    proc: subprocess.Popen


class ReplayFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StopSignal:
    """Sticky stop flag. A pipe, because Event.set() can deadlock inside a handler."""

    def __init__(self):
        self.signum = None
        self._read, self._write = os.pipe()
        for signum in STOP_SIGNALS:
            signal.signal(signum, self._handle)

    def _handle(self, signum, _frame):
        if self.signum is None:
            self.signum = signum
            os.write(self._write, b"\0")

    def wait(self, timeout: float) -> bool:
        return bool(select.select([self._read], [], [], timeout)[0])


def log(message: str):
    print(f"serve_replay: {message}", flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="historical SCADA CSV")
    parser.add_argument("--logs-source", type=Path, default=LOGS_DEFAULT, help="publisher event CSV")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help="SQLite historian")
    parser.add_argument("--modbus-port", type=int, default=MODBUS_PORT_DEFAULT)
    parser.add_argument("--hmi-port", type=int, default=HMI_PORT_DEFAULT)
    parser.add_argument("--replay-interval", type=float, default=REPLAY_INTERVAL_DEFAULT,
                        help="wall-clock seconds per ten-minute source record (default 10)")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_DEFAULT,
                        help="collector seconds between Modbus polls (default 0.5)")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS,
                        help="seconds without a fresh sequence before STALE (default 25)")
    parser.add_argument("--startup-timeout", type=float, default=30.0,
                        help="seconds each child gets to become ready (default 30)")
    parser.add_argument("--stop-timeout", type=float, default=5.0,
                        help="seconds each child gets to exit before SIGKILL (default 5)")
    args = parser.parse_args(argv)

    durations = (args.replay_interval, args.poll_interval, args.max_age, args.startup_timeout, args.stop_timeout)
    if not all(math.isfinite(value) and value > 0 for value in durations):
        parser.error("intervals and timeouts must be positive finite numbers")
    if args.max_age <= max(args.replay_interval, args.poll_interval):
        parser.error("--max-age must exceed both --replay-interval and --poll-interval")
    if not all(1 <= port <= 65535 for port in (args.modbus_port, args.hmi_port)):
        parser.error("ports must be between 1 and 65535")
    if args.modbus_port == args.hmi_port:
        parser.error("--modbus-port and --hmi-port must differ")
    args.source, args.logs_source, args.db = (path.resolve() for path in (args.source, args.logs_source, args.db))
    return args


def child_commands(args: argparse.Namespace) -> dict[str, list[str]]:
    python = [sys.executable, "-u", "-m"]
    return {
        "gateway": [*python, "scripts.ot_gateway", "--host", HOST, "--port", str(args.modbus_port),
                    "--source", str(args.source), "--interval", str(args.replay_interval)],
        "collector": [*python, "scripts.scada_collector", "--host", HOST, "--port", str(args.modbus_port),
                      "--db", str(args.db), "--logs-source", str(args.logs_source),
                      "--interval", str(args.poll_interval), "--max-age", str(args.max_age)],
        "hmi": [*python, "scripts.hmi", "--db", str(args.db), "--port", str(args.hmi_port)],
    }


def exit_status(returncode: int) -> int:
    """Status for a child that was not asked to exit; a clean exit is still a failure."""
    if returncode < 0:
        return 128 - returncode
    return returncode or 1


def _die_with_runner():
    # Children sit in their own session so a terminal Ctrl-C cannot kill the gateway
    # before the collector; this keeps SIGKILL of the runner from orphaning them.
    _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def spawn(name: str, command: list[str]) -> Child:
    proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                            start_new_session=True, preexec_fn=_die_with_runner)
    log(f"started {name} pid={proc.pid}")
    return Child(name, proc)


def stop_children(children: list[Child], grace: float):
    """Stop newest first: a collector outliving the gateway would record a false COMM_LOSS."""
    for child in reversed(children):
        child.proc.terminate()
        try:
            child.proc.wait(grace)
        except subprocess.TimeoutExpired:
            log(f"{child.name} ignored SIGTERM for {grace}s; sending SIGKILL")
            child.proc.kill()
            try:
                child.proc.wait(grace)
            except subprocess.TimeoutExpired:
                log(f"{child.name} pid={child.proc.pid} still present after SIGKILL")


def owns_listener(pid: int, port: int) -> bool:
    address = f"{int.from_bytes(socket.inet_aton(HOST), sys.byteorder):08X}:{port:04X}"
    with open("/proc/net/tcp", encoding="ascii") as table:
        next(table)
        rows = [line.split() for line in table]
    sockets = {f"socket:[{row[9]}]" for row in rows if row[1] == address and row[3] == TCP_LISTEN}
    if not sockets:
        return False
    with contextlib.suppress(FileNotFoundError):  # the child or one of its fds has just gone
        with os.scandir(f"/proc/{pid}/fd") as entries:
            return any(os.readlink(entry.path) in sockets for entry in entries)
    return False


def gateway_answers(port: int) -> bool:
    client = ModbusTcpClient(HOST, port=port, timeout=PROBE_TIMEOUT, retries=0)
    try:
        if not client.connect():
            return False
        for device_id in IDS:
            response = client.read_input_registers(0, count=WORDS, device_id=device_id)
            if response.isError():
                return False
            decode_registers(response.registers)
        return True
    except (ModbusException, OSError, ValueError, IndexError):
        return False
    finally:
        client.close()


def historian_fresh(db: Path, since: datetime) -> bool:
    """All turbines have an observation from this run, not one left by an earlier run."""
    try:
        with contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=PROBE_TIMEOUT)) as conn:
            rows = conn.execute("SELECT turbine_id,observed_at_utc FROM current WHERE sequence IS NOT NULL").fetchall()
    except sqlite3.OperationalError:  # the collector has not created the file or schema yet
        return False
    return {turbine for turbine, seen in rows if datetime.fromisoformat(seen) >= since} == set(IDS.values())


def hmi_answers(port: int) -> bool:
    conn = http.client.HTTPConnection(HOST, port, timeout=PROBE_TIMEOUT)
    try:
        conn.request("GET", "/api/fleet")
        response = conn.getresponse()
        fleet = json.loads(response.read())
        return response.status == 200 and fleet["mode"] == HMI_MODE and set(fleet["turbines"]) == set(IDS.values())
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return False
    finally:
        conn.close()


def require_running(children: list[Child]):
    for child in children:
        returncode = child.proc.poll()
        if returncode is not None:
            raise ReplayFailure(exit_status(returncode), f"{child.name} exited unexpectedly with {returncode}")


def await_ready(children: list[Child], ready, timeout: float, stop: StopSignal) -> bool:
    """False if a stop signal arrived first; raises if a child dies or time runs out."""
    deadline = time.monotonic() + timeout
    while not stop.wait(0):
        require_running(children)
        if ready():
            return True
        if time.monotonic() >= deadline:
            raise ReplayFailure(1, f"{children[-1].name} not ready within {timeout:g}s")
        stop.wait(PROBE_INTERVAL)
    return False


def start_stack(args: argparse.Namespace, children: list[Child], stop: StopSignal) -> bool:
    commands = child_commands(args)

    children.append(spawn("gateway", commands["gateway"]))
    gateway_pid = children[-1].proc.pid
    if not await_ready(children, lambda: owns_listener(gateway_pid, args.modbus_port)
                       and gateway_answers(args.modbus_port), args.startup_timeout, stop):
        return False

    since = datetime.now(timezone.utc)
    children.append(spawn("collector", commands["collector"]))
    # an unchanged sequence is not recorded, so a restart can need one replay tick
    if not await_ready(children, lambda: historian_fresh(args.db, since),
                       args.startup_timeout + args.replay_interval, stop):
        return False

    children.append(spawn("hmi", commands["hmi"]))
    hmi_pid = children[-1].proc.pid
    return await_ready(children, lambda: owns_listener(hmi_pid, args.hmi_port) and hmi_answers(args.hmi_port),
                       args.startup_timeout, stop)


def run(args: argparse.Namespace) -> int:
    stop = StopSignal()
    children: list[Child] = []
    try:
        if start_stack(args, children, stop):
            log(f"REPLAY_READY modbus={HOST}:{args.modbus_port} hmi=http://{HOST}:{args.hmi_port}/ read-only")
            while not stop.wait(MONITOR_INTERVAL):
                require_running(children)
        log(f"stopping on {signal.Signals(stop.signum).name}")
        return 0
    except ReplayFailure as failure:
        log(f"FAILED: {failure}")
        return failure.status
    finally:
        stop_children(children, args.stop_timeout)
        log("all children stopped")


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
