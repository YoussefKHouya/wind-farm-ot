"""Level 1 metering: MU readings, IED checks and the RTU data table over Modbus TCP.

One process emulates the chain metering unit -> IED -> RTU. It samples Level 0
metering values, the IED compares each upstream/downstream pair, and the RTU
publishes the result as holding registers (default 127.0.0.1:15022, device 1).

Holding registers, project-defined (not IEC 61850/MMS or a vendor map):
  turbine block base 0/10/20 (T01/T02/T03): +0 power kW, +1 generator kV x100,
    +2 generator A, +3 downstream A, +4 rotor RPM x10, +5 bearing C x10 (signed),
    +6 wind m/s x10, +7 breaker closed, +8 IED alert bits
  farm block: 30 transformer in kW, 31 transformer out kW, 32 feeder kV x10,
    33 feeder A x10, 34 export kW, 35 feeder closed, 36 feeder IED alert bits,
    37 sequence, 38 quality (1 good, 0 field link lost: values held)

IED alert bits: 1 leakage (breaker closed, upstream exceeds downstream),
2 breaker inconsistent (open breaker carrying current), 4 overcurrent,
8 value out of physical range.

By default the data table accepts Modbus writes, like an RTU whose data memory is
writable over its engineering protocol: this is the deliberate weakness for the
false-data-injection exercise. Each refresh overwrites injected values, so an
attacker must keep writing. ``--reject-writes`` is the hardened configuration.
"""
import argparse
import asyncio
import math

from scripts.controller_io import ModbusTables, read_field, serve_forever

TURBINES = ("T01", "T02", "T03")
TURBINE_FIELDS = ("power_kw", "gen_kv_x100", "gen_a", "downstream_a", "rpm_x10",
                  "bearing_temp_x10", "wind_x10", "breaker_closed", "alerts")
FARM_FIELDS = ("transformer_in_kw", "transformer_out_kw", "feeder_kv_x10", "feeder_a_x10",
               "export_kw", "feeder_closed", "alerts", "sequence", "quality")
HR = {t: {name: 10 * i + offset for offset, name in enumerate(TURBINE_FIELDS)} for i, t in enumerate(TURBINES)}
HR["farm"] = {name: 30 + offset for offset, name in enumerate(FARM_FIELDS)}
TABLE_SIZE = 50
ALERT_LEAKAGE, ALERT_BREAKER_INCONSISTENT, ALERT_OVERCURRENT, ALERT_OUT_OF_RANGE = 1, 2, 4, 8
TURBINE_ALARM_A = 1900.0      # 95 % of the 2,000 A breaker rating
FEEDER_ALARM_A = 110.0
GEN_KV_MAX, FEEDER_KV_MAX = 0.76, 36.3  # 110 % of nominal
RTU_PORT_DEFAULT = 15022


def _u16(value: float) -> int:
    return max(0, min(65535, int(round(value))))


def _s16(value: float) -> int:
    return max(-32768, min(32767, int(round(value)))) & 0xFFFF


def ied_alerts(snap: dict) -> dict:
    sensors, meters = snap["sensors"], snap["meters"]
    alerts = {}
    for t in TURBINES:
        m, closed, bits = meters["turbines"][t], sensors["breaker_closed"][t], 0
        up, down = m["gen_a"], m["downstream_a"]
        if closed and up - down > max(20.0, 0.05 * up):
            bits |= ALERT_LEAKAGE
        if not closed and down > 1.0:
            bits |= ALERT_BREAKER_INCONSISTENT
        if up > TURBINE_ALARM_A:
            bits |= ALERT_OVERCURRENT
        temp = m["bearing_temp_c"]
        if not 0 <= m["gen_kv"] <= GEN_KV_MAX or (temp is not None and not -40 <= temp <= 150):
            bits |= ALERT_OUT_OF_RANGE
        alerts[t] = bits
    closed, bits = sensors["breaker_closed"]["FDR"], 0
    out_kw, export_kw = meters["transformer"]["out_kw"], meters["export_kw"]
    if closed and out_kw - export_kw > max(10.0, 0.02 * out_kw):
        bits |= ALERT_LEAKAGE
    if not closed and export_kw > 0:
        bits |= ALERT_BREAKER_INCONSISTENT
    if meters["feeder"]["current_a"] > FEEDER_ALARM_A:
        bits |= ALERT_OVERCURRENT
    if not 0 <= meters["feeder"]["kv"] <= FEEDER_KV_MAX:
        bits |= ALERT_OUT_OF_RANGE
    alerts["FDR"] = bits
    return alerts


def rtu_table(snap: dict, sequence: int) -> list[int]:
    table = [0] * TABLE_SIZE
    sensors, meters, alerts = snap["sensors"], snap["meters"], ied_alerts(snap)
    for t in TURBINES:
        m, hr = meters["turbines"][t], HR[t]
        table[hr["power_kw"]] = _u16(m["power_kw"])
        table[hr["gen_kv_x100"]] = _u16(m["gen_kv"] * 100)
        table[hr["gen_a"]] = _u16(m["gen_a"])
        table[hr["downstream_a"]] = _u16(m["downstream_a"])
        table[hr["rpm_x10"]] = _u16(m["rpm"] * 10)
        table[hr["bearing_temp_x10"]] = _s16((m["bearing_temp_c"] or 0) * 10)
        table[hr["wind_x10"]] = _u16(m["wind_mps"] * 10)
        table[hr["breaker_closed"]] = int(bool(sensors["breaker_closed"][t]))
        table[hr["alerts"]] = alerts[t]
    farm = HR["farm"]
    table[farm["transformer_in_kw"]] = _u16(meters["transformer"]["in_kw"])
    table[farm["transformer_out_kw"]] = _u16(meters["transformer"]["out_kw"])
    table[farm["feeder_kv_x10"]] = _u16(meters["feeder"]["kv"] * 10)
    table[farm["feeder_a_x10"]] = _u16(meters["feeder"]["current_a"] * 10)
    table[farm["export_kw"]] = _u16(meters["export_kw"])
    table[farm["feeder_closed"]] = int(bool(sensors["breaker_closed"]["FDR"]))
    table[farm["alerts"]] = alerts["FDR"]
    table[farm["sequence"]] = sequence % 65536
    table[farm["quality"]] = 1
    return table


async def run(args):
    tables = ModbusTables(hr=TABLE_SIZE, writable_hr=not args.reject_writes)
    state = {"sequence": 0}

    async def refresh():
        snap = await read_field(args.field_port)
        if snap is None:
            tables.hr[HR["farm"]["quality"]] = 0  # hold last values, flag them bad
            return
        state["sequence"] += 1
        tables.hr[:] = rtu_table(snap, state["sequence"])

    mode = "read-only" if args.reject_writes else "writable-data-table"
    await serve_forever(tables.server("127.0.0.1", args.port),
                        f"RTU_READY modbus=127.0.0.1:{args.port} field=127.0.0.1:{args.field_port} {mode}",
                        refresh, args.scan)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=RTU_PORT_DEFAULT)
    parser.add_argument("--field-port", type=int, default=15030)
    parser.add_argument("--scan", type=float, default=0.5, help="metering refresh period in seconds")
    parser.add_argument("--reject-writes", action="store_true", help="hardened: refuse Modbus writes")
    args = parser.parse_args()
    if not math.isfinite(args.scan) or args.scan <= 0:
        parser.error("scan must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
