"""Separate Modbus TCP polling process and SQLite historian.

Only Modbus observations cross into the collector. An outage produces a
COMM_LOSS marker with null values; it never fabricates 0 production. Poll
clock is UTC; source timestamps remain unzoned strings from the publisher.
"""
import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from pymodbus.client import AsyncModbusTcpClient

from scripts.ot_protocol import WORDS, decode_registers
from scripts.ot_gateway import IDS
from scripts.simulate import ROOT

DB_DEFAULT = ROOT / "data/historian/control_room_week.sqlite"
LOGS_DEFAULT = ROOT / "data/raw/greek_logs_3t_2020-02-05_11.csv"
DEFAULT_MAX_AGE_SECONDS = 25.0  # must exceed the ten-second replay tick


class Historian:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, turbine_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, observed_at_utc TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS current (
                turbine_id TEXT PRIMARY KEY, sequence INTEGER,
                observed_at_utc TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, turbine_id TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_events (
                id INTEGER PRIMARY KEY, turbine_id TEXT NOT NULL,
                source_timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def _current(self, turbine):
        row = self.conn.execute("SELECT payload FROM current WHERE turbine_id=?", (turbine,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, device_id: int, values: dict, observed_at_utc: str,
               max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
        turbine = IDS[device_id]
        previous = self._current(turbine)
        last = self.conn.execute(
            "SELECT sequence,observed_at_utc,payload FROM observations WHERE turbine_id=? ORDER BY id DESC LIMIT 1",
            (turbine,)).fetchone()
        payload = {"turbine_id": turbine, "observed_at_utc": observed_at_utc, **values}
        changed = last is None or last[0] != values["sequence"]
        if last and not changed:
            last_payload = json.loads(last[2])
            if any(last_payload.get(key) != value for key, value in values.items()):
                raise ValueError("Modbus payload changed without a new sequence")
        recovered = previous and previous["quality"] in ("COMM_LOSS", "BAD_DATA", "STALE") and changed
        with self.conn:
            if changed:
                self.conn.execute("INSERT INTO observations (turbine_id,sequence,observed_at_utc,payload) VALUES (?,?,?,?)",
                                  (turbine, values["sequence"], observed_at_utc, json.dumps(payload)))
                self.conn.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,?)",
                                  (turbine, values["sequence"], observed_at_utc, json.dumps(payload)))
                if recovered:
                    self.conn.execute("INSERT INTO events (turbine_id,observed_at_utc,kind) VALUES (?,?,?)",
                                      (turbine, observed_at_utc, "COMM_RECOVERED"))
            elif last and (datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
                           - datetime.fromisoformat(last[1].replace("Z", "+00:00"))).total_seconds() > max_age_seconds:
                if previous and previous["quality"] not in ("STALE", "COMM_LOSS", "BAD_DATA"):
                    self.mark_unavailable(device_id, observed_at_utc, "STALE")
        return changed

    def mark_unavailable(self, device_id: int, observed_at_utc: str, kind: str = "COMM_LOSS"):
        if kind not in ("COMM_LOSS", "BAD_DATA", "STALE"):
            raise ValueError("invalid quality marker")
        turbine = IDS[device_id]
        previous = self._current(turbine)
        if previous and previous["quality"] == kind:
            return
        payload = {"turbine_id": turbine, "sequence": None,
                   "observed_at_utc": observed_at_utc,
                   "source_timestamp": previous.get("source_timestamp") if previous else None,
                   "wind_mps": None, "bearing_temp_c": None, "rotor_rpm": None,
                   "source_power_labelled_w": None, "quality": kind}
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,?)",
                              (turbine, None, observed_at_utc, json.dumps(payload)))
            self.conn.execute("INSERT INTO events (turbine_id,observed_at_utc,kind) VALUES (?,?,?)",
                              (turbine, observed_at_utc, kind))

    def latest(self) -> dict:
        return {row[0]: json.loads(row[1]) for row in self.conn.execute("SELECT turbine_id,payload FROM current")}

    def history(self, turbine_id: str, limit: int = 144) -> list[dict]:
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT payload FROM observations WHERE turbine_id=? ORDER BY id DESC LIMIT ?", (turbine_id, limit))]

    def events(self, limit: int = 50) -> list[dict]:
        return [dict(zip(("turbine_id", "observed_at_utc", "kind"), row)) for row in
                self.conn.execute("SELECT turbine_id,observed_at_utc,kind FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def import_source_events(self, path: Path) -> int:
        """Idempotent import of recorded publisher events via a separate file channel."""
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        with self.conn:
            prior = self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
            if prior:
                if prior != len(rows):
                    raise ValueError("historian source events differ from input file")
                stored = [json.loads(r[0]) for r in
                          self.conn.execute("SELECT payload FROM source_events ORDER BY id")]
                if stored != rows:
                    raise ValueError("historian source events differ from input file")
                return 0
            for index, row in enumerate(rows, 1):
                if row["source_turbine_id"] not in IDS.values() or not row["Detected"]:
                    raise ValueError("invalid source event")
                self.conn.execute("INSERT INTO source_events VALUES (?,?,?,?,?)",
                                  (index, row["source_turbine_id"], row["Detected"],
                                   row["Event type"], json.dumps(row)))
        return len(rows)

    def recorded_events(self) -> dict:
        latest = self.latest()
        rows = []
        for record in self.conn.execute("SELECT payload FROM source_events ORDER BY source_timestamp,turbine_id,id"):
            row = json.loads(record[0])
            timestamp = latest.get(row["source_turbine_id"], {}).get("source_timestamp")
            if timestamp and row["Detected"] <= timestamp:
                rows.append(row)
        alarms = [row for row in rows if row["Event type"] in ("Alarm log (A)", "Warning log (W)")]
        return {"events": rows[-30:], "alarms": alarms[-30:],
                "quality": "RECORDED_SOURCE_EVENTS" if self.conn.execute("SELECT 1 FROM source_events LIMIT 1").fetchone() else "SOURCE_EVENTS_UNAVAILABLE"}

    def close(self):
        self.conn.close()


async def poll_once(host: str, port: int, db: Historian, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    client = AsyncModbusTcpClient(host, port=port, timeout=0.7, retries=0)
    try:
        if not await client.connect():
            raise ConnectionError("Modbus endpoint unavailable")
        for device_id in IDS:
            try:
                response = await client.read_input_registers(0, count=WORDS, device_id=device_id)
                if response.isError():
                    raise ValueError("Modbus exception response")
                db.record(device_id, decode_registers(response.registers), now, max_age_seconds)
            except (ValueError, IndexError, KeyError, TimeoutError, OSError):
                db.mark_unavailable(device_id, now, "BAD_DATA")
    except (ConnectionError, OSError, TimeoutError):
        for device_id in IDS:
            db.mark_unavailable(device_id, now, "COMM_LOSS")
    finally:
        client.close()


async def main_async(args):
    db = Historian(args.db)
    try:
        db.import_source_events(args.logs_source)
        while True:
            await poll_once(args.host, args.port, db, args.max_age)
            await asyncio.sleep(args.interval)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=15020)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--logs-source", type=Path, default=LOGS_DEFAULT,
                        help="publisher event CSV; imported to historian separately from Modbus")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS,
                        help="seconds without a fresh sequence before STALE (default 25)")
    args = parser.parse_args()
    if args.interval <= 0 or args.max_age <= args.interval:
        parser.error("interval must be positive and max-age must exceed interval")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
