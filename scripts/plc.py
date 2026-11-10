"""Level 1 PLC for the simulated wind farm: run requests and motorised breaker closers.

Modbus TCP on loopback (default 127.0.0.1:15021, device ID 1). Project-defined map:

  Coils (FC01 read, FC05/15 write)   0-2 RUN T01-T03   3-5 CB-T01..T03 closer   6 CB-FDR closer
  Discrete inputs (FC02)              0-3 CB-T01..T03, CB-FDR position   4-6 safety latched T01-T03
  Input registers (FC04)              0-2 turbine state code   3 field link OK   4 scan counter
                                      5 field tick (mod 65536)

Ladder logic each scan: read field contacts over Level 0 field I/O; on the first
successful scan copy breaker positions and running states into the coils (so a
PLC restart never drops a running plant); force RUN low while a safety latch is
set (interlock: a START needs a fresh rising edge after reset); then drive the
field outputs from the coils. Coil writes are unauthenticated, as in Modbus.
Not a vendor PLC program.
"""
import argparse
import asyncio
import math

from scripts.controller_io import ModbusTables, field_request, read_field, serve_forever

TURBINES = ("T01", "T02", "T03")
COIL_RUN = {"T01": 0, "T02": 1, "T03": 2}
COIL_BREAKER = {"T01": 3, "T02": 4, "T03": 5, "FDR": 6}
DI_BREAKER = {"T01": 0, "T02": 1, "T03": 2, "FDR": 3}
DI_SAFETY = {"T01": 4, "T02": 5, "T03": 6}
IR_STATE = {"T01": 0, "T02": 1, "T03": 2}
IR_COMM, IR_SCAN, IR_TICK = 3, 4, 5
N_COILS, N_DI, N_IR = 7, 7, 8
STATE_CODES = {"RUNNING": 1, "STOPPED": 2, "LOW_WIND": 3, "HIGH_WIND_CUTOUT": 4, "DISCONNECTED": 5,
               "GRID_LOSS": 6, "SAFETY_TRIPPED": 7, "SAFETY_UNAVAILABLE": 8}
STATE_NAMES = {code: name for name, code in STATE_CODES.items()}
NOT_RUNNING = ("STOPPED", "SAFETY_TRIPPED", "SAFETY_UNAVAILABLE")
PLC_PORT_DEFAULT = 15021


def plc_scan(coils: list[bool], snap: dict | None, first_scan: bool):
    """One ladder scan. Returns (coils, field outputs or None, discrete inputs, input registers)."""
    coils = list(coils)
    if snap is None:
        return coils, None, [False] * N_DI, [0] * N_IR
    sensors = snap["sensors"]
    if first_scan:  # first-scan bit: adopt field state instead of forcing defaults
        for name, index in COIL_BREAKER.items():
            coils[index] = bool(sensors["breaker_closed"][name])
        for turbine_id, index in COIL_RUN.items():
            coils[index] = sensors["turbine_state"][turbine_id] not in NOT_RUNNING
    for turbine_id, index in COIL_RUN.items():
        if sensors["safety_latched"][turbine_id]:
            coils[index] = False
    outputs = {"run": {t: coils[i] for t, i in COIL_RUN.items()},
               "breaker": {n: int(coils[i]) for n, i in COIL_BREAKER.items()}}
    di = [False] * N_DI
    for name, index in DI_BREAKER.items():
        di[index] = bool(sensors["breaker_closed"][name])
    for turbine_id, index in DI_SAFETY.items():
        di[index] = bool(sensors["safety_latched"][turbine_id])
    ir = [0] * N_IR
    for turbine_id, index in IR_STATE.items():
        ir[index] = STATE_CODES.get(sensors["turbine_state"][turbine_id], 0)
    ir[IR_COMM] = 1
    ir[IR_TICK] = int(snap["tick"]) % 65536
    return coils, outputs, di, ir


async def run(args):
    tables = ModbusTables(coils=N_COILS, di=N_DI, ir=N_IR, hr=1, writable_coils=True)
    state = {"first": True, "scans": 0}

    async def scan():
        snap = await read_field(args.field_port)
        coils, outputs, di, ir = plc_scan(tables.coils, snap, state["first"])
        tables.coils[:] = coils
        state["scans"] += 1
        ir[IR_SCAN] = state["scans"] % 65536
        tables.di[:], tables.ir[:] = di, ir
        if outputs is not None:
            state["first"] = False
            if await field_request(args.field_port, {"op": "write", "outputs": outputs}) is None:
                tables.ir[IR_COMM] = 0

    await serve_forever(tables.server("127.0.0.1", args.port),
                        f"PLC_READY modbus=127.0.0.1:{args.port} field=127.0.0.1:{args.field_port}",
                        scan, args.scan)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=PLC_PORT_DEFAULT)
    parser.add_argument("--field-port", type=int, default=15030)
    parser.add_argument("--scan", type=float, default=0.2, help="ladder scan period in seconds")
    args = parser.parse_args()
    if not math.isfinite(args.scan) or args.scan <= 0:
        parser.error("scan must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
