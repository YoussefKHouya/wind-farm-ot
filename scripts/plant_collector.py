"""Level 2 plant SCADA collector: polls the PLC and RTU over Modbus TCP into the plant historian.

Each poll reads the PLC (coils, discrete inputs, input registers) and the RTU
(holding-register data table) and records one sample per controller with a
quality flag:

  GOOD             fresh, valid values
  STALE            the controller answers but its scan counter / sequence has not
                   moved for longer than --max-age (a frozen controller)
  COMM_LOSS        the controller did not answer
  BAD_DATA         it answered with an exception or values outside the register map
  FIELD_LINK_LOST  it answered but reports that it lost its own field inputs

Any non-GOOD state nulls the current values (never a fabricated zero) and keeps
the last good samples in history. Recovery needs a genuinely new sample. The
collector only observes: it never writes to a controller. Endpoints are
configurable (host:port) so the same code runs on one host or across VMs.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from scripts.plc import (COIL_BREAKER, COIL_RUN, DI_BREAKER, DI_SAFETY, IR_COMM, IR_SCAN, IR_STATE,
                         IR_TICK, N_COILS, N_DI, N_IR, STATE_NAMES)
from scripts.rtu import HR, TABLE_SIZE, TURBINES
from scripts.simulate import ROOT

DEVICES = ("plc", "rtu")
UNAVAILABLE = ("STALE", "COMM_LOSS", "BAD_DATA", "FIELD_LINK_LOST")
DB_DEFAULT = ROOT / "data/historian/plant.sqlite"
DEFAULT_MAX_AGE_SECONDS = 5.0
MAX_ALERT_BITS = 15


def parse_endpoint(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"endpoint must be host:port, got {text!r}")
    return host, int(port)


def _seconds(observed_at_utc: str) -> float:
    return datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00")).timestamp()


def _signed(raw: int) -> int:
    return raw - 65536 if raw >= 32768 else raw


def decode_plc(coils, di, ir) -> dict:
    if len(coils) < N_COILS or len(di) < N_DI or len(ir) < N_IR:
        raise ValueError("PLC frame too short")
    field_link = ir[IR_COMM] == 1
    if ir[IR_COMM] not in (0, 1):
        raise ValueError("PLC field-link flag outside the map")
    states = {}
    for t, index in IR_STATE.items():
        code = ir[index]
        if code in STATE_NAMES:
            states[t] = STATE_NAMES[code]
        elif code == 0 and not field_link:
            states[t] = None
        else:
            raise ValueError(f"unknown turbine state code {code}")
    return {
        "run_request": {t: bool(coils[i]) for t, i in COIL_RUN.items()},
        "breaker_command": {n: bool(coils[i]) for n, i in COIL_BREAKER.items()},
        "breaker_closed": {n: bool(di[i]) for n, i in DI_BREAKER.items()},
        "safety_latched": {t: bool(di[i]) for t, i in DI_SAFETY.items()},
        "turbine_state": states,
        "field_link": field_link, "scan": ir[IR_SCAN], "field_tick": ir[IR_TICK],
    }


def decode_rtu(hr) -> dict:
    if len(hr) < TABLE_SIZE:
        raise ValueError("RTU frame too short")
    turbines = {}
    for t in TURBINES:
        m = HR[t]
        if hr[m["breaker_closed"]] not in (0, 1) or hr[m["alerts"]] > MAX_ALERT_BITS:
            raise ValueError(f"RTU {t} values outside the map")
        turbines[t] = {
            "power_kw": hr[m["power_kw"]], "gen_kv": hr[m["gen_kv_x100"]] / 100,
            "gen_a": hr[m["gen_a"]], "downstream_a": hr[m["downstream_a"]],
            "rpm": hr[m["rpm_x10"]] / 10, "bearing_temp_c": _signed(hr[m["bearing_temp_x10"]]) / 10,
            "wind_mps": hr[m["wind_x10"]] / 10, "breaker_closed": bool(hr[m["breaker_closed"]]),
            "alerts": hr[m["alerts"]],
        }
    f = HR["farm"]
    if hr[f["feeder_closed"]] not in (0, 1) or hr[f["quality"]] not in (0, 1) or hr[f["alerts"]] > MAX_ALERT_BITS:
        raise ValueError("RTU farm values outside the map")
    return {
        "turbines": turbines,
        "farm": {"transformer_in_kw": hr[f["transformer_in_kw"]], "transformer_out_kw": hr[f["transformer_out_kw"]],
                 "feeder_kv": hr[f["feeder_kv_x10"]] / 10, "feeder_a": hr[f["feeder_a_x10"]] / 10,
                 "export_kw": hr[f["export_kw"]], "feeder_closed": bool(hr[f["feeder_closed"]]),
                 "alerts": hr[f["alerts"]]},
        "sequence": hr[f["sequence"]], "field_link": hr[f["quality"]] == 1,
    }


def _change_key(device: str, values: dict) -> int:
    return values["scan"] if device == "plc" else values["sequence"]


class PlantHistorian:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, device TEXT NOT NULL, change_key INTEGER NOT NULL,
                observed_at_utc TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_device ON samples(device, id);
            CREATE TABLE IF NOT EXISTS current (
                device TEXT PRIMARY KEY, quality TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, device TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL, kind TEXT NOT NULL);
        """)
        self.conn.commit()

    @staticmethod
    def _check(device: str):
        if device not in DEVICES:
            raise ValueError(f"unknown device {device!r}")

    def _quality(self, device):
        row = self.conn.execute("SELECT quality FROM current WHERE device=?", (device,)).fetchone()
        return row[0] if row else None

    def record(self, device: str, values: dict, observed_at_utc: str,
               max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
        """Store a decoded sample. Returns True only when a new sample was written."""
        self._check(device)
        if not values["field_link"]:
            self.mark(device, "FIELD_LINK_LOST", observed_at_utc)
            return False
        key = _change_key(device, values)
        last = self.conn.execute("SELECT change_key, observed_at_utc FROM samples WHERE device=? "
                                 "ORDER BY id DESC LIMIT 1", (device,)).fetchone()
        previous = self._quality(device)
        if last is None or last[0] != key:
            payload = json.dumps(values)
            with self.conn:
                self.conn.execute("INSERT INTO samples (device, change_key, observed_at_utc, payload) "
                                  "VALUES (?,?,?,?)", (device, key, observed_at_utc, payload))
                self.conn.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,?)",
                                  (device, "GOOD", observed_at_utc, payload))
                if previous in UNAVAILABLE:
                    self.conn.execute("INSERT INTO events (device, observed_at_utc, kind) VALUES (?,?,?)",
                                      (device, observed_at_utc, "RECOVERED"))
            return True
        if previous == "GOOD" and _seconds(observed_at_utc) - _seconds(last[1]) > max_age_seconds:
            self.mark(device, "STALE", observed_at_utc)
        return False

    def mark(self, device: str, kind: str, observed_at_utc: str):
        self._check(device)
        if kind not in UNAVAILABLE:
            raise ValueError(f"invalid quality {kind!r}")
        if self._quality(device) == kind:
            return
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,NULL)", (device, kind, observed_at_utc))
            self.conn.execute("INSERT INTO events (device, observed_at_utc, kind) VALUES (?,?,?)",
                              (device, observed_at_utc, kind))

    def latest(self) -> dict:
        return {device: {"quality": quality, "observed_at_utc": seen,
                         "values": json.loads(payload) if payload else None}
                for device, quality, seen, payload in self.conn.execute("SELECT * FROM current")}

    def history(self, device: str, limit: int = 200) -> list[dict]:
        self._check(device)
        return [{"observed_at_utc": seen, "values": json.loads(payload)} for seen, payload in self.conn.execute(
            "SELECT observed_at_utc, payload FROM samples WHERE device=? ORDER BY id DESC LIMIT ?", (device, limit))]

    def events(self, limit: int = 50) -> list[dict]:
        return [{"device": d, "observed_at_utc": seen, "kind": kind} for d, seen, kind in self.conn.execute(
            "SELECT device, observed_at_utc, kind FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def close(self):
        self.conn.close()


class _BadResponse(Exception):
    pass


async def _read_plc(client):
    coils = await client.read_coils(0, count=N_COILS, device_id=1)
    di = await client.read_discrete_inputs(0, count=N_DI, device_id=1)
    ir = await client.read_input_registers(0, count=N_IR, device_id=1)
    if any(r.isError() for r in (coils, di, ir)):
        raise _BadResponse("PLC exception response")
    return decode_plc(coils.bits, di.bits, ir.registers)


async def _read_rtu(client):
    hr = await client.read_holding_registers(0, count=TABLE_SIZE, device_id=1)
    if hr.isError():
        raise _BadResponse("RTU exception response")
    return decode_rtu(hr.registers)


async def _poll_device(device, endpoint, reader, db, now, max_age_seconds):
    client = AsyncModbusTcpClient(endpoint[0], port=endpoint[1], timeout=0.7, retries=0)
    try:
        if not await client.connect():
            db.mark(device, "COMM_LOSS", now)
            return
        try:
            values = await reader(client)
        except (_BadResponse, ValueError, IndexError):
            db.mark(device, "BAD_DATA", now)
            return
        db.record(device, values, now, max_age_seconds)
    except (ModbusException, OSError, asyncio.TimeoutError, ConnectionError):
        db.mark(device, "COMM_LOSS", now)
    finally:
        client.close()


async def poll_once(plc: tuple[str, int], rtu: tuple[str, int], db: PlantHistorian,
                    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    await asyncio.gather(_poll_device("plc", plc, _read_plc, db, now, max_age_seconds),
                         _poll_device("rtu", rtu, _read_rtu, db, now, max_age_seconds))


async def run(args):
    db = PlantHistorian(args.db)
    try:
        print(f"COLLECTOR_READY plc={args.plc[0]}:{args.plc[1]} rtu={args.rtu[0]}:{args.rtu[1]} db={args.db}",
              flush=True)
        while True:
            await poll_once(args.plc, args.rtu, db, args.max_age)
            await asyncio.sleep(args.interval)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plc", type=parse_endpoint, default=("127.0.0.1", 15021), help="PLC host:port")
    parser.add_argument("--rtu", type=parse_endpoint, default=("127.0.0.1", 15022), help="RTU host:port")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS,
                        help="seconds without a new sample before STALE")
    args = parser.parse_args()
    if not (math.isfinite(args.interval) and args.interval > 0 and math.isfinite(args.max_age)
            and args.max_age > args.interval):
        parser.error("interval must be positive and max-age must exceed interval")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
