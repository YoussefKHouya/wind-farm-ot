"""Shared plumbing for Level 1 controllers: field I/O link and Modbus data tables."""
import asyncio
import json
import socket

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import SimData, SimDevice
from pymodbus.simulator.simdata import DataType

FIELD_KEYS = ("tick", "sim_time_utc", "sensors", "meters")


async def field_request(port: int, message: dict, timeout: float = 0.5) -> dict | None:
    """One UDP request to the Level 0 field process; None on loss or malformed reply."""
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            await loop.sock_sendto(sock, json.dumps(message).encode(), ("127.0.0.1", port))
            data = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout)
            reply = json.loads(data)
        except (OSError, asyncio.TimeoutError, ValueError):
            return None
    if not isinstance(reply, dict) or "error" in reply:
        return None
    return reply


async def read_field(port: int) -> dict | None:
    snap = await field_request(port, {"op": "read"})
    return snap if snap and all(key in snap for key in FIELD_KEYS) else None


class ModbusTables:
    """Coil / discrete-input / holding / input tables served through a SimDevice action hook.

    The controller logic owns these lists; every Modbus read is answered from them and
    every accepted write lands in them. Modbus itself carries no authentication.
    """

    def __init__(self, coils=1, di=1, hr=1, ir=1, writable_coils=False, writable_hr=False):
        self.coils, self.di = [False] * coils, [False] * di
        self.hr, self.ir = [0] * hr, [0] * ir
        self.writable_coils, self.writable_hr = writable_coils, writable_hr

    @staticmethod
    def _pack(bits, regs):
        for word in range(len(regs)):
            regs[word] = sum(1 << i for i in range(16)
                             if word * 16 + i < len(bits) and bits[word * 16 + i])

    @staticmethod
    def _unpack(regs, bits):
        for index in range(len(bits)):
            bits[index] = bool(regs[index // 16] >> (index % 16) & 1)

    async def action(self, function_code, _start, address, _count, regs, set_values):
        if set_values is not None:  # a write: validate, then apply to the controller's own table
            if function_code in (5, 15):
                if not self.writable_coils or address + len(set_values) > len(self.coils):
                    return ExcCodes.ILLEGAL_ADDRESS
                self.coils[address:address + len(set_values)] = [bool(v) for v in set_values]
            elif function_code in (6, 16):
                if not self.writable_hr or address + len(set_values) > len(self.hr):
                    return ExcCodes.ILLEGAL_ADDRESS
                self.hr[address:address + len(set_values)] = [int(v) & 0xFFFF for v in set_values]
            else:
                return ExcCodes.ILLEGAL_ADDRESS
            return None
        # pymodbus' own store is never trusted: reads are always answered from our tables
        if function_code == 1:
            self._pack(self.coils, regs)
        elif function_code == 2:
            self._pack(self.di, regs)
        elif function_code == 3:
            regs[:len(self.hr)] = self.hr
        elif function_code == 4:
            regs[:len(self.ir)] = self.ir
        return None

    def server(self, host: str, port: int, device_id: int = 1) -> ModbusTcpServer:
        def bits(n):
            return [SimData(0, values=[False] * max(n, 16), datatype=DataType.BITS)]

        def regs(n):
            return [SimData(0, values=[0] * n, datatype=DataType.REGISTERS)]

        device = SimDevice(id=device_id, action=self.action,
                           simdata=(bits(len(self.coils)), bits(len(self.di)),
                                    regs(len(self.hr)), regs(len(self.ir))))
        return ModbusTcpServer([device], address=(host, port))


async def serve_forever(server: ModbusTcpServer, ready_line: str, loop_body, interval: float):
    task = asyncio.create_task(server.serve_forever())
    try:
        await asyncio.sleep(0)
        if task.done():
            task.result()
        print(ready_line, flush=True)
        while True:
            await loop_body()
            await asyncio.sleep(interval)
    finally:
        await server.shutdown()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
