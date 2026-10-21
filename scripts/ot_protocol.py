"""Versioned *project* Modbus register contract for published SCADA replay.

One TCP endpoint; device IDs 1..3 map to source IDs WT01..WT03. Read input
registers FC04 at zero-based address 0, count 13. Never a vendor register map.
Power unit follows the source XLSX label [W] without a speculative conversion.
Source clock timezone is unspecified; poll time is separately recorded as UTC.
"""
from datetime import datetime
import math

VERSION = 1
WORDS = 13
FIELD_WIND = "Ambient WindSpeed Avg. [m/s]"
FIELD_TEMP = "Generator Bearing Temp. Avg. [°C]"
FIELD_RPM = "Rotor RPM Avg. [RPM]"
FIELD_POWER = "Grid Production Power Avg. [W]"


def _scaled(value, low, high, missing):
    if value in ("", None):
        return missing, False
    number = float(value)
    scaled = round(number * 10)
    if not math.isfinite(number) or not low <= scaled <= high:
        raise ValueError(f"source value out of register range: {value!r}")
    return scaled, True


def encode_sample(row: dict, sequence: int) -> list[int]:
    if not 1 <= sequence <= 65535:
        raise ValueError("sequence out of register range")
    timestamp = row["timestamp_source"]
    try:
        moment = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid source timestamp (timezone unspecified)") from exc
    if moment.strftime("%Y-%m-%d %H:%M:%S") != timestamp:
        raise ValueError("invalid source timestamp format")
    wind, wind_valid = _scaled(row[FIELD_WIND], 0, 65534, 65535)
    temp, temp_valid = _scaled(row[FIELD_TEMP], -32767, 32767, -32768)
    rpm, rpm_valid = _scaled(row[FIELD_RPM], 0, 65534, 65535)
    power, power_valid = _scaled(row[FIELD_POWER], -(2**31-1), 2**31-1, -(2**31))
    flag = (int(str(row["missing_data"]).lower() == "true")
            | (wind_valid << 1) | (temp_valid << 2) | (rpm_valid << 3)
            | (power_valid << 4))
    return [VERSION, sequence, wind, temp & 0xffff, rpm,
            (power >> 16) & 0xffff, power & 0xffff, flag,
            moment.year, moment.month, moment.day, moment.hour, moment.minute]


def decode_registers(values: list[int]) -> dict:
    if len(values) != WORDS or any(not 0 <= n <= 65535 for n in values):
        raise ValueError("invalid Modbus frame length/values")
    if values[0] != VERSION:
        raise ValueError("unsupported register-map version")
    if values[1] < 1:
        raise ValueError("uninitialized sequence")
    try:
        moment = datetime(values[8], values[9], values[10], values[11], values[12])
    except ValueError as exc:
        raise ValueError("invalid source clock in Modbus registers") from exc
    mask = values[7]
    if mask & ~31:
        raise ValueError("unknown quality flags")
    def valid(bit):
        return bool(mask & (1 << bit))
    def signed16(raw):
        return raw - 65536 if raw >= 32768 else raw
    def signed32(raw):
        return raw - (1 << 32) if raw >= (1 << 31) else raw
    packed = (values[5] << 16) | values[6]
    if ((not valid(1) and values[2] != 65535)
            or (not valid(2) and values[3] != 0x8000)
            or (not valid(3) and values[4] != 65535)
            or (not valid(4) and packed != 0x80000000)):
        raise ValueError("missing-value sentinel/quality mismatch")
    return {
        "sequence": values[1],
        "source_timestamp": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "wind_mps": values[2] / 10 if valid(1) else None,
        "bearing_temp_c": signed16(values[3]) / 10 if valid(2) else None,
        "rotor_rpm": values[4] / 10 if valid(3) else None,
        "source_power_labelled_w": signed32(packed) / 10 if valid(4) else None,
        "quality": "SOURCE_MISSING" if mask & 1 or mask & 30 != 30 else "GOOD",
    }
