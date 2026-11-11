"""Level 2 alarm engine: rules over the plant historian, debounce, alarm lifecycle and audit journal.

Each cycle reads the latest PLC and RTU samples written by the plant collector, evaluates the
rules below, and updates alarm instances in their own database (default
data/historian/alarms.sqlite). It never talks to a controller.

Rules (key -> priority):
  COLLECTOR_STALE                   HIGH      no collector heartbeat for --stale-after seconds
  COMM:plc / COMM:rtu               HIGH      controller data quality is not GOOD
  SAFETY:<T>                        CRITICAL  safety trip latched (first-out: hides its breaker mismatch)
  SAFETY_UNAVAILABLE:<T>            CRITICAL  the safety system itself is unavailable
  BREAKER:<CB>                      HIGH      closer command and position contact disagree
  IED:<unit>:<alert>                MED/HIGH  RTU IED alert bits
  RANGE:<field>                     MEDIUM    physically impossible RTU value
  ENERGY_BALANCE                    HIGH      turbine output x transformer efficiency != export meter
  CROSSCHECK:BREAKER:<CB>           HIGH      PLC and RTU report different breaker positions
  CROSSCHECK:POWER:<T>              HIGH      PLC turbine state and RTU power disagree

The last four are the false-data-injection detectors: a forged RTU value must stay consistent
with the PLC, the energy balance and physics, or it raises an alarm. They detect inconsistency,
not intent: a metering fault produces the same alarm.

Lifecycle (ISA-18.2 style): a condition must hold for its on-delay cycles before raising and be
absent for --off-delay cycles before clearing. An alarm stays listed until it is both cleared and
acknowledged; a returning condition reactivates an open alarm and needs a new acknowledgement.
While a source's data is bad, its alarms are held rather than falsely cleared.
"""
import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
import time

from scripts.plant_collector import DB_DEFAULT as PLANT_DB_DEFAULT, PlantHistorian
from scripts.rtu import TURBINES
from scripts.simulate import ROOT

DB_DEFAULT = ROOT / "data/historian/alarms.sqlite"
PRIORITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
BREAKERS = {"CB-T01": "T01", "CB-T02": "T02", "CB-T03": "T03", "CB-FDR": "FDR"}
IED_BITS = {1: ("LEAKAGE", "MEDIUM"), 2: ("BREAKER_INCONSISTENT", "HIGH"),
            4: ("OVERCURRENT", "HIGH"), 8: ("OUT_OF_RANGE", "MEDIUM")}
HELD_BY = {"plc": ("SAFETY", "BREAKER:", "CROSSCHECK:"),
           "rtu": ("IED:", "RANGE:", "ENERGY_BALANCE", "CROSSCHECK:")}
TRANSFORMER_EFFICIENCY = 0.99
TURBINE_RATED_KW = 2000
RANGES = {"gen_kv": (0.0, 0.76), "bearing_temp_c": (-40.0, 150.0), "power_kw": (0, 1.05 * TURBINE_RATED_KW),
          "rpm": (0.0, 20.0)}
FEEDER_KV_MAX = 36.3
STALE_AFTER_DEFAULT = 10.0


def _condition(priority, message, on_delay):
    return {"priority": priority, "message": message, "on_delay": on_delay}


def evaluate(latest: dict, heartbeat: float | None, now: float, stale_after: float = STALE_AFTER_DEFAULT):
    """Return (conditions currently true, key prefixes whose state cannot be judged right now)."""
    if heartbeat is None or now - heartbeat > stale_after:
        return {"COLLECTOR_STALE": _condition("HIGH", "SCADA collector stopped: plant data is not being updated",
                                              1)}, {""}
    conditions, held = {}, set()
    good = {}
    for device in ("plc", "rtu"):
        sample = latest.get(device)
        quality = sample["quality"] if sample else "NO_DATA"
        if quality == "GOOD" and sample.get("values"):
            good[device] = sample["values"]
        else:
            conditions[f"COMM:{device}"] = _condition("HIGH", f"{device.upper()} data quality {quality}", 1)
            held.update(HELD_BY[device])
    plc, rtu = good.get("plc"), good.get("rtu")

    if plc:
        for t in TURBINES:
            if plc["turbine_state"][t] == "SAFETY_UNAVAILABLE":
                conditions[f"SAFETY_UNAVAILABLE:{t}"] = _condition(
                    "CRITICAL", f"{t} safety system unavailable: turbine held stopped", 1)
            elif plc["safety_latched"][t]:
                conditions[f"SAFETY:{t}"] = _condition(
                    "CRITICAL", f"{t} safety trip latched: local supervisor reset required", 1)
        for breaker, unit in BREAKERS.items():
            if unit in TURBINES and plc["safety_latched"][unit]:
                continue  # first-out: the trip already explains the open breaker
            commanded, closed = plc["breaker_command"][unit], plc["breaker_closed"][unit]
            if commanded and not closed:
                conditions[f"BREAKER:{breaker}"] = _condition("HIGH", f"{breaker} commanded closed but open (trip?)", 3)
            elif closed and not commanded:
                conditions[f"BREAKER:{breaker}"] = _condition("HIGH", f"{breaker} commanded open but still closed", 3)

    if rtu:
        units = {**{t: rtu["turbines"][t]["alerts"] for t in TURBINES}, "FDR": rtu["farm"]["alerts"]}
        for unit, bits in units.items():
            for bit, (name, priority) in IED_BITS.items():
                if bits & bit:
                    conditions[f"IED:{unit}:{name}"] = _condition(priority, f"RTU IED alert {name} on {unit}", 2)
        for t in TURBINES:
            for field, (low, high) in RANGES.items():
                value = rtu["turbines"][t][field]
                if not low <= value <= high:
                    conditions[f"RANGE:{t}:{field}"] = _condition(
                        "MEDIUM", f"{t} {field}={value} outside physical range {low}..{high}", 2)
        farm = rtu["farm"]
        if not 0 <= farm["feeder_kv"] <= FEEDER_KV_MAX:
            conditions["RANGE:feeder_kv"] = _condition(
                "MEDIUM", f"feeder voltage {farm['feeder_kv']} kV outside 0..{FEEDER_KV_MAX} kV", 2)
        if farm["feeder_closed"]:
            expected = sum(rtu["turbines"][t]["power_kw"] for t in TURBINES) * TRANSFORMER_EFFICIENCY
            if abs(farm["export_kw"] - expected) > max(30.0, 0.03 * expected):
                conditions["ENERGY_BALANCE"] = _condition(
                    "HIGH", f"export meter {farm['export_kw']} kW vs turbines {expected:.0f} kW", 3)

    if plc and rtu:
        for breaker, unit in BREAKERS.items():
            rtu_closed = rtu["farm"]["feeder_closed"] if unit == "FDR" else rtu["turbines"][unit]["breaker_closed"]
            if rtu_closed != plc["breaker_closed"][unit]:
                conditions[f"CROSSCHECK:BREAKER:{breaker}"] = _condition(
                    "HIGH", f"{breaker}: PLC position {plc['breaker_closed'][unit]} vs RTU {rtu_closed}", 3)
        for t in TURBINES:
            running, producing = plc["turbine_state"][t] == "RUNNING", rtu["turbines"][t]["power_kw"] > 0
            if running != producing:
                conditions[f"CROSSCHECK:POWER:{t}"] = _condition(
                    "HIGH", f"{t}: PLC state {plc['turbine_state'][t]} vs RTU power "
                            f"{rtu['turbines'][t]['power_kw']} kW", 3)
    return conditions, held


class AlarmStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, priority TEXT NOT NULL, message TEXT NOT NULL,
                active INTEGER NOT NULL, acked INTEGER NOT NULL, closed INTEGER NOT NULL,
                raised_at TEXT NOT NULL, cleared_at TEXT, acked_at TEXT, acked_by TEXT);
            CREATE INDEX IF NOT EXISTS alarms_open ON alarms(closed, key);
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, alarm_id INTEGER NOT NULL,
                key TEXT NOT NULL, event TEXT NOT NULL, actor TEXT);
        """)
        self.conn.commit()

    def _open(self, key):
        return self.conn.execute("SELECT id, active, acked FROM alarms WHERE key=? AND closed=0", (key,)).fetchone()

    def _journal(self, at, alarm_id, key, event, actor=None):
        self.conn.execute("INSERT INTO journal (at_utc, alarm_id, key, event, actor) VALUES (?,?,?,?,?)",
                          (at, alarm_id, key, event, actor))

    def active_keys(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT key FROM alarms WHERE closed=0 AND active=1")}

    def raise_alarm(self, key, priority, message, at):
        with self.conn:
            row = self._open(key)
            if row and row[1]:
                return
            if row:  # cleared but unacknowledged: reactivate, and require a new acknowledgement
                self.conn.execute("UPDATE alarms SET active=1, acked=0, acked_at=NULL, acked_by=NULL, "
                                  "cleared_at=NULL, priority=?, message=? WHERE id=?", (priority, message, row[0]))
                alarm_id = row[0]
            else:
                alarm_id = self.conn.execute(
                    "INSERT INTO alarms (key, priority, message, active, acked, closed, raised_at) "
                    "VALUES (?,?,?,1,0,0,?)", (key, priority, message, at)).lastrowid
            self._journal(at, alarm_id, key, "RAISED")

    def clear_alarm(self, key, at):
        with self.conn:
            row = self._open(key)
            if not row or not row[1]:
                return
            self.conn.execute("UPDATE alarms SET active=0, cleared_at=?, closed=acked WHERE id=?", (at, row[0]))
            self._journal(at, row[0], key, "CLEARED")

    def acknowledge(self, alarm_id: int, actor: str, at: str) -> bool:
        with self.conn:
            row = self.conn.execute("SELECT key, active FROM alarms WHERE id=? AND closed=0 AND acked=0",
                                    (alarm_id,)).fetchone()
            if not row:
                return False
            self.conn.execute("UPDATE alarms SET acked=1, acked_at=?, acked_by=?, closed=? WHERE id=?",
                              (at, actor, int(not row[1]), alarm_id))
            self._journal(at, alarm_id, row[0], "ACKED", actor)
            return True

    def open_alarms(self) -> list[dict]:
        order = " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(PRIORITIES))
        rows = self.conn.execute(
            "SELECT id, key, priority, message, active, acked, raised_at, cleared_at, acked_at, acked_by "
            f"FROM alarms WHERE closed=0 ORDER BY CASE priority {order} END, raised_at, id").fetchall()
        names = ("id", "key", "priority", "message", "active", "acked", "raised_at", "cleared_at", "acked_at",
                 "acked_by")
        result = []
        for row in rows:
            alarm = dict(zip(names, row))
            alarm["active"], alarm["acked"] = bool(alarm["active"]), bool(alarm["acked"])
            result.append(alarm)
        return result

    def journal(self, limit: int = 100) -> list[dict]:
        return [dict(zip(("at_utc", "alarm_id", "key", "event", "actor"), row)) for row in self.conn.execute(
            "SELECT at_utc, alarm_id, key, event, actor FROM journal ORDER BY id DESC LIMIT ?", (limit,))]

    def close(self):
        self.conn.close()


class AlarmEngine:
    """Debounce conditions into alarm transitions; state survives restarts through the store."""

    def __init__(self, store: AlarmStore, off_delay: int = 2):
        self.store = store
        self.off_delay = off_delay
        self.on_counts: dict[str, int] = {}
        self.off_counts: dict[str, int] = {}

    def step(self, conditions: dict, held: set[str], at: str):
        active = self.store.active_keys()
        for key, condition in conditions.items():
            self.off_counts.pop(key, None)
            if key in active:
                continue
            self.on_counts[key] = self.on_counts.get(key, 0) + 1
            if self.on_counts[key] >= condition["on_delay"]:
                self.store.raise_alarm(key, condition["priority"], condition["message"], at)
                self.on_counts.pop(key)
        for key in list(self.on_counts):
            if key not in conditions:
                del self.on_counts[key]  # debounce restarts when the condition drops out
        for key in active - set(conditions):
            if any(key.startswith(prefix) for prefix in held):
                continue  # data is bad: hold the alarm rather than clear it on missing evidence
            self.off_counts[key] = self.off_counts.get(key, 0) + 1
            if self.off_counts[key] >= self.off_delay:
                self.store.clear_alarm(key, at)
                del self.off_counts[key]


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plant-db", type=Path, default=PLANT_DB_DEFAULT, help="plant historian (collector output)")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help="alarm database")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between evaluations")
    parser.add_argument("--stale-after", type=float, default=STALE_AFTER_DEFAULT,
                        help="seconds without a collector heartbeat before COLLECTOR_STALE")
    parser.add_argument("--off-delay", type=int, default=2, help="clean cycles before an alarm clears")
    args = parser.parse_args()
    if not (math.isfinite(args.interval) and args.interval > 0 and args.stale_after > args.interval
            and args.off_delay >= 1):
        parser.error("interval must be positive, stale-after must exceed interval, off-delay >= 1")
    plant = PlantHistorian(args.plant_db)
    store = AlarmStore(args.db)
    engine = AlarmEngine(store, args.off_delay)
    print(f"ALARMS_READY plant={args.plant_db} db={args.db}", flush=True)
    try:
        while True:
            conditions, held = evaluate(plant.latest(), plant.last_heartbeat(), time.time(), args.stale_after)
            engine.step(conditions, held, _now_text())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        plant.close()
        store.close()


if __name__ == "__main__":
    main()
