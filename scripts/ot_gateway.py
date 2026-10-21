"""Read-only, loopback Modbus TCP gateway for real historical SCADA replay.

Device IDs 1-3 = WT01-WT03; FC04/FC03 address 0 count 13. Values are
source observations. This is a purpose-built project protocol, not a wind-turbine
OEM register layout or a control/safety system. No Modbus writes permitted.
"""
import argparse
import asyncio
import csv
from functools import partial
from pathlib import Path

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import SimData, SimDevice
from pymodbus.simulator.simdata import DataType

from scripts.ot_protocol import WORDS, encode_sample
from scripts.replay_scada import INPUT_DEFAULT

IDS = {1: "WT01", 2: "WT02", 3: "WT03"}


def load_source(path: Path, limit: int | None = None, start: int = 0) -> dict[int, list[dict]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "source_turbine_id" not in reader.fieldnames:
            raise ValueError("CSV missing source_turbine_id")
        rows = list(reader)
    grouped = {n: [row for row in rows if row["source_turbine_id"] == turbine]
               for n, turbine in IDS.items()}
    counts = {len(group) for group in grouped.values()}
    if len(counts) != 1 or not counts or 0 in counts or any(row["source_turbine_id"] not in IDS.values() for row in rows):
        raise ValueError("all three source turbines must have equal nonempty series")
    count = counts.pop()
    if not 0 <= start < count:
        raise ValueError("start index outside source series")
    if limit is not None:
        if not 1 <= limit <= count - start:
            raise ValueError("limit outside source series")
        count = start + limit
    for n, group in grouped.items():
        grouped[n] = group[start:count]
        stamps = [row["timestamp_source"] for row in grouped[n]]
        if stamps != sorted(set(stamps)):
            raise ValueError("source turbine timestamps must be unique and increasing")
    if len({tuple(row["timestamp_source"] for row in group) for group in grouped.values()}) != 1:
        raise ValueError("source turbine timestamps not aligned")
    return grouped


class Gateway:
    def __init__(self, grouped: dict[int, list[dict]]):
        self.grouped = grouped
        self.snapshot = {n: encode_sample(rows[0], 1) for n, rows in grouped.items()}
        self.devices = [SimDevice(id=n,
                                  simdata=SimData(0, values=[0] * WORDS,
                                                  datatype=DataType.REGISTERS, readonly=True),
                                  action=partial(self._read, n)) for n in IDS]

    async def _read(self, n, function_code, start_address, address, count, current_registers, set_values):
        if function_code not in (3, 4) or set_values is not None:
            return ExcCodes.ILLEGAL_FUNCTION
        current_registers[:WORDS] = self.snapshot[n]
        return None

    async def serve(self, host="127.0.0.1", port=15020, interval=1.0, loop=False):
        if interval <= 0:
            raise ValueError("interval must be positive")
        server = ModbusTcpServer(self.devices, address=(host, port))
        task = asyncio.create_task(server.serve_forever())
        try:
            await asyncio.sleep(0)  # let the actual listener bind before READY
            if task.done():
                task.result()
            print(f"MODBUS_READY {host}:{port} devices=1,2,3 count={WORDS} read-only", flush=True)
            sequence = 0
            while True:
                for index in range(len(self.grouped[1])):
                    sequence = sequence % 65535 + 1
                    self.snapshot = {n: encode_sample(rows[index], sequence)
                                     for n, rows in self.grouped.items()}
                    await asyncio.sleep(interval)
                if not loop:
                    await task  # hold final observation online until stopped
        finally:
            await server.shutdown()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=INPUT_DEFAULT)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=15020)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--limit", type=int, help="source ticks to replay (one tick per selected turbine)")
    parser.add_argument("--start-index", type=int, default=0, help="zero-based source tick to start from")
    parser.add_argument("--loop", action="store_true", help="repeat the historical slice; sequence remains monotonic")
    args = parser.parse_args()
    asyncio.run(Gateway(load_source(args.source, args.limit, args.start_index)).serve(args.host, args.port, args.interval, args.loop))


if __name__ == "__main__":
    main()
