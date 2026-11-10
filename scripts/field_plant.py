"""Level 0 field process of the simulated wind farm (the "physical world").

Three weather-driven turbines (windpowerlib OEDB V90/2000 curve) each feed a
motorised 690 V generator breaker; a 0.69/33 kV step-up transformer and a 33 kV
feeder breaker carry the farm export to the grid-side export meter.

Interfaces, all local:
* Field I/O over loopback UDP JSON (op ``read``/``write``) emulates the wiring
  between field devices and Level 1 controllers: breaker position contacts,
  turbine state and metering-unit values out; run requests and breaker closer
  coils in. Like real field wiring it has no authentication.
* The safety function runs in a private child process (scripts.safety_service)
  fed by a dedicated simulated bearing sensor. A latched trip stops the turbine,
  trips and blocks its breaker, and cannot be cleared over field I/O, Modbus or
  the HMI.
* LOCAL_RESET is accepted only on an owner-only Unix socket (the local console),
  from the same Unix user, with a named supervisor, and every attempt is audited.

Breakers follow the motorised-closer pattern: a 0->1 coil pulse closes, 1->0
opens, and a trip leaves the coil high, so coil and position can disagree.
This is an educational simulation: not an OEM model, a certified SIS, or a
physical safety boundary.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import struct

import pandas as pd
from windpowerlib import ModelChain, WindTurbine

from scripts.safety_service import SafetyClient
from scripts.simulate import (HUB_HEIGHT_M, INJECTED_BEARING_TEMP_C, ROOT, TURBINE_MODEL,
                              TURBINES, load_weather)

GEN_KV = 0.69
FEEDER_KV = 33.0
POWER_FACTOR = 0.95
TRANSFORMER_EFFICIENCY = 0.99
TURBINE_BREAKER_MAX_A = 2000.0
FEEDER_BREAKER_MAX_A = 120.0
HIGH_WIND_CUTOUT_MPS = 25.0
FEEDER = "FDR"
FAULT_KINDS = ("overheat", "sensor_failure", "overcurrent")
SUPERVISOR_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
FIELD_PORT_DEFAULT = 15030
WEATHER_DEFAULT = ROOT / "data/raw/tarfaya_weather_2024-01-01_07.csv"
RUNTIME = ROOT / "data/runtime"


def line_current_a(power_kw: float, kv: float) -> float:
    return power_kw * 1000 / (math.sqrt(3) * kv * 1000 * POWER_FACTOR) if power_kw > 0 else 0.0


class Breaker:
    """Motorised circuit breaker: closer coil, position contact, overcurrent trip."""

    def __init__(self, name: str, max_current_a: float):
        self.name = name
        self.max_current_a = max_current_a
        self.closed = False
        self.motor = 0
        self.blocked = False
        self.trip_reason = None

    def set_motor(self, value: int):
        if value not in (0, 1):
            raise ValueError("breaker motor value must be 0 or 1")
        if value == self.motor:
            return
        self.motor = value
        if value == 1 and not self.blocked:
            self.closed, self.trip_reason = True, None
        elif value == 0:
            self.closed = False

    def trip(self, reason: str):
        if self.closed:
            self.closed = False
            self.trip_reason = reason

    def carry(self, current_a: float) -> bool:
        if not self.closed:
            return False
        if current_a >= self.max_current_a:
            self.trip("OVERCURRENT")
            return False
        return True


class WindFarmField:
    """Deterministic tick-by-tick field process; one tick per weather record."""

    def __init__(self, weather: list[dict], safety: SafetyClient):
        if not weather:
            raise ValueError("weather series is empty")
        self.weather = weather
        self.safety = safety
        self.tick = 0
        model = WindTurbine(turbine_type=TURBINE_MODEL, hub_height=HUB_HEIGHT_M)
        self.rated_kw = model.nominal_power / 1000
        index = pd.to_datetime([row["timestamp_utc"] for row in weather], utc=True)
        self.curves, self.winds = {}, {}
        for turbine_id, factor in TURBINES.items():
            winds = [float(row["wind_speed_100m_mps"]) * factor for row in weather]
            frame = pd.DataFrame({("wind_speed", HUB_HEIGHT_M): winds}, index=index)
            self.curves[turbine_id] = [max(0.0, float(v) / 1000)
                                       for v in ModelChain(model).run_model(frame).power_output]
            self.winds[turbine_id] = winds
        self.breakers = {t: Breaker(f"CB-{t}", TURBINE_BREAKER_MAX_A) for t in TURBINES}
        self.breakers[FEEDER] = Breaker("CB-FDR", FEEDER_BREAKER_MAX_A)
        self.run = {t: False for t in TURBINES}
        self.needs_start = {t: False for t in TURBINES}
        self.temp = {t: None for t in TURBINES}
        self.load = {t: 0.0 for t in TURBINES}
        self.faults: dict[str, str] = {}
        self.safety_status = {t: {"latched": False, "reason": None} for t in TURBINES}
        self.safety_available = True
        self.state = {t: "STOPPED" for t in TURBINES}
        self.meters = self._zero_meters()

    # -- inputs ------------------------------------------------------------------------
    def apply_outputs(self, outputs: dict):
        """Apply Level 1 controller outputs: run requests and breaker closer coils."""
        if not isinstance(outputs, dict):
            raise ValueError("outputs must be an object")
        run = outputs.get("run", {})
        coils = outputs.get("breaker", {})
        if (not isinstance(run, dict) or not isinstance(coils, dict)
                or set(run) - set(TURBINES) or set(coils) - {*TURBINES, FEEDER}):
            raise ValueError("unknown turbine or breaker")
        if any(not isinstance(v, bool) for v in run.values()):
            raise ValueError("run request must be boolean")
        if any(v not in (0, 1) or isinstance(v, bool) for v in coils.values()):
            raise ValueError("breaker coil must be 0 or 1")
        for turbine_id, requested in run.items():
            if requested and not self.run[turbine_id] and not self.safety_status[turbine_id]["latched"]:
                self.needs_start[turbine_id] = False  # a fresh START after a trip reset
            self.run[turbine_id] = requested
        for name, value in coils.items():
            self.breakers[name].set_motor(value)

    def inject_fault(self, turbine_id: str, kind: str):
        """Scenario-only one-shot fault applied on the next tick."""
        if turbine_id not in TURBINES or kind not in FAULT_KINDS:
            raise ValueError("invalid fault")
        self.faults[turbine_id] = kind

    def local_reset(self, turbine_id: str, supervisor: str) -> dict:
        if turbine_id not in TURBINES:
            raise ValueError("unknown turbine")
        if not isinstance(supervisor, str) or not SUPERVISOR_RE.fullmatch(supervisor):
            raise ValueError("named supervisor required")
        if not self.safety_available:
            return {"accepted": False, "reason": "SAFETY_SERVICE_UNAVAILABLE"}
        try:
            result = self.safety.reset(turbine_id, local_request=True)
        except RuntimeError:
            self.safety_available = False
            return {"accepted": False, "reason": "SAFETY_SERVICE_UNAVAILABLE"}
        self.safety_status[turbine_id] = {"latched": result["latched"], "reason": result["reason"]}
        if result["reset_accepted"]:
            self.breakers[turbine_id].blocked = False
            return {"accepted": True, "reason": None}
        reason = "NOT_TRIPPED" if not result["latched"] else "SENSOR_NOT_BELOW_RESET_SETPOINT"
        return {"accepted": False, "reason": reason}

    # -- process -----------------------------------------------------------------------
    def step(self) -> dict:
        index = min(self.tick, len(self.weather) - 1)
        ambient = float(self.weather[index]["ambient_temperature_2m_c"])
        faults, self.faults = self.faults, {}
        safety_temps = {}
        for t in TURBINES:
            prior = self.temp[t] if self.temp[t] is not None else ambient + 10
            temp = prior + 0.4 * (ambient + 10 + 15 * self.load[t] - prior)
            if faults.get(t) == "overheat":
                temp = max(temp, INJECTED_BEARING_TEMP_C)
            self.temp[t] = temp
            safety_temps[t] = None if faults.get(t) == "sensor_failure" else temp
        if self.safety_available:
            try:
                for t in TURBINES:
                    status = self.safety.sample(t, safety_temps[t])
                    self.safety_status[t] = {"latched": status["latched"], "reason": status["reason"]}
            except RuntimeError:
                self.safety_available = False
        if not self.safety_available:  # no protection decision is no permission to generate
            for t in TURBINES:
                self.safety_status[t] = {"latched": True, "reason": "SAFETY_SERVICE_UNAVAILABLE"}

        feeder = self.breakers[FEEDER]
        turbines = {}
        for t in TURBINES:
            breaker = self.breakers[t]
            wind, potential = self.winds[t][index], self.curves[t][index]
            power = 0.0
            if self.safety_status[t]["latched"]:
                breaker.trip("SAFETY_TRIP")
                breaker.blocked = True
                self.needs_start[t] = True
                state = "SAFETY_UNAVAILABLE" if not self.safety_available else "SAFETY_TRIPPED"
            elif not self.run[t] or self.needs_start[t]:
                state = "STOPPED"
            elif wind >= HIGH_WIND_CUTOUT_MPS:
                state = "HIGH_WIND_CUTOUT"
            elif potential <= 0:
                state = "LOW_WIND"
            elif not breaker.closed:
                state = "DISCONNECTED"
            elif not feeder.closed:
                state = "GRID_LOSS"
            else:
                current = line_current_a(potential, GEN_KV)
                if faults.get(t) == "overcurrent":
                    current = breaker.max_current_a * 1.2
                if breaker.carry(current):
                    state, power = "RUNNING", potential
                else:
                    state = "DISCONNECTED"
            self.state[t] = state
            self.load[t] = min(1.0, power / self.rated_kw)
            gen_a = line_current_a(power, GEN_KV)
            turbines[t] = {
                "wind_mps": round(wind, 3), "power_kw": power,
                "gen_kv": GEN_KV if power > 0 else 0.0, "gen_a": gen_a,
                "downstream_a": gen_a if breaker.closed else 0.0,
                "rpm": 15.0 * math.sqrt(power / self.rated_kw) if power > 0 else 0.0,
                "bearing_temp_c": round(self.temp[t], 3),
            }
        transformer_in = sum(item["power_kw"] for item in turbines.values())
        transformer_out = transformer_in * TRANSFORMER_EFFICIENCY
        feeder_a = line_current_a(transformer_out, FEEDER_KV)
        exported = transformer_out if feeder.carry(feeder_a) else 0.0
        self.meters = {
            "turbines": turbines, "ambient_c": ambient,
            "transformer": {"in_kw": transformer_in, "out_kw": transformer_out,
                            "out_kv": FEEDER_KV if transformer_out > 0 else 0.0},
            "feeder": {"kv": FEEDER_KV, "current_a": line_current_a(exported, FEEDER_KV)},
            "export_kw": exported,
        }
        self.tick += 1
        return self.snapshot()

    def _zero_meters(self) -> dict:
        return {"turbines": {t: {"wind_mps": 0.0, "power_kw": 0.0, "gen_kv": 0.0, "gen_a": 0.0,
                                 "downstream_a": 0.0, "rpm": 0.0, "bearing_temp_c": None}
                             for t in TURBINES},
                "ambient_c": None,
                "transformer": {"in_kw": 0.0, "out_kw": 0.0, "out_kv": 0.0},
                "feeder": {"kv": FEEDER_KV, "current_a": 0.0}, "export_kw": 0.0}

    def snapshot(self) -> dict:
        index = min(max(self.tick - 1, 0), len(self.weather) - 1)
        names = [*TURBINES, FEEDER]
        return {
            "tick": self.tick, "sim_time_utc": self.weather[index]["timestamp_utc"],
            "sensors": {
                "breaker_closed": {n: self.breakers[n].closed for n in names},
                "breaker_trip_reason": {n: self.breakers[n].trip_reason for n in names},
                "turbine_state": dict(self.state),
                "safety_latched": {t: self.safety_status[t]["latched"] for t in TURBINES},
                "safety_reason": {t: self.safety_status[t]["reason"] for t in TURBINES},
            },
            "meters": self.meters,
        }


# -- process wiring ----------------------------------------------------------------------

class FieldIO(asyncio.DatagramProtocol):
    def __init__(self, field: WindFarmField, allow_faults: bool):
        self.field = field
        self.allow_faults = allow_faults

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            request = json.loads(data)
            op = request.get("op") if isinstance(request, dict) else None
            if op == "read":
                reply = self.field.snapshot()
            elif op == "write":
                self.field.apply_outputs(request.get("outputs"))
                reply = {"ok": True}
            elif op == "fault" and self.allow_faults:
                self.field.inject_fault(request.get("turbine_id"), request.get("kind"))
                reply = {"ok": True}
            else:
                reply = {"error": "unsupported field I/O operation"}
        except (ValueError, TypeError, AttributeError) as exc:
            reply = {"error": str(exc) or "invalid request"}
        self.transport.sendto(json.dumps(reply, allow_nan=False).encode(), addr)


def _peer_uid(writer) -> int | None:
    sock = writer.get_extra_info("socket")
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                              struct.calcsize("3i")))
        return uid
    except (OSError, AttributeError):
        return None


def console_handler(field: WindFarmField, audit_log: Path):
    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            uid = _peer_uid(writer)
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("op") != "reset":
                    raise ValueError("unsupported console operation")
                if uid != os.getuid():
                    raise ValueError("local console accepts only the plant's own user")
                turbine_id, supervisor = request.get("turbine_id"), request.get("supervisor")
                try:
                    result = field.local_reset(turbine_id, supervisor)
                    reply = {"turbine_id": turbine_id, **result}
                except ValueError as exc:
                    result, reply = {"accepted": False, "reason": str(exc)}, {"error": str(exc)}
                entry = {"at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                         "sim_time_utc": field.snapshot()["sim_time_utc"], "action": "LOCAL_RESET",
                         "turbine_id": turbine_id, "supervisor": supervisor if isinstance(supervisor, str) else None,
                         "peer_uid": uid, **result}
                with audit_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
            except ValueError as exc:
                reply = {"error": str(exc)}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
    return handle


async def serve(args):
    with SafetyClient() as safety:
        field = WindFarmField(load_weather(args.weather), safety)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: FieldIO(field, args.scenario_faults), local_addr=("127.0.0.1", args.port))
        args.console_socket.parent.mkdir(parents=True, exist_ok=True)
        args.audit_log.parent.mkdir(parents=True, exist_ok=True)
        args.console_socket.unlink(missing_ok=True)
        previous = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(console_handler(field, args.audit_log),
                                                     path=str(args.console_socket))
        finally:
            os.umask(previous)
        os.chmod(args.console_socket, 0o600)
        print(f"FIELD_READY udp=127.0.0.1:{args.port} console={args.console_socket} "
              f"scenario_faults={args.scenario_faults}", flush=True)
        try:
            while True:
                await asyncio.sleep(args.interval)
                field.step()
        finally:
            transport.close()
            server.close()
            args.console_socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weather", type=Path, default=WEATHER_DEFAULT)
    parser.add_argument("--port", type=int, default=FIELD_PORT_DEFAULT)
    parser.add_argument("--interval", type=float, default=2.0,
                        help="wall-clock seconds per hourly weather record (default 2)")
    parser.add_argument("--console-socket", type=Path, default=RUNTIME / "local_console.sock")
    parser.add_argument("--audit-log", type=Path, default=RUNTIME / "safety_audit.jsonl")
    parser.add_argument("--scenario-faults", action="store_true",
                        help="accept scenario fault injection over field I/O (exercises only)")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("interval must be positive")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
