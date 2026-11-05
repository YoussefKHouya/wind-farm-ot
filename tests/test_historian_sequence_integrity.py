"""Historian invariants for one Modbus sequence during historical replay."""

import asyncio

import pytest

from scripts.ot_protocol import decode_registers, encode_sample
from scripts.scada_collector import Historian, poll_once


def sample(*, power="100", timestamp="2020-02-05 10:20:00"):
    return {
        "source_turbine_id": "WT01", "timestamp_source": timestamp,
        "Ambient WindSpeed Avg. [m/s]": "12", "Generator Bearing Temp. Avg. [°C]": "40",
        "Rotor RPM Avg. [RPM]": "9", "Grid Production Power Avg. [W]": power,
        "missing_data": "False",
    }


def reading(*, sequence=1, power="100", timestamp="2020-02-05 10:20:00"):
    return decode_registers(encode_sample(sample(power=power, timestamp=timestamp), sequence))


def test_same_sequence_with_changed_measurement_is_rejected_without_mutating_history(tmp_path):
    db = Historian(tmp_path / "historian.sqlite")
    try:
        assert db.record(1, reading(), "2026-01-01T00:00:00Z") is True
        with pytest.raises(ValueError, match="sequence"):
            db.record(1, reading(power="999"), "2026-01-01T00:00:01Z")
        assert db.latest()["WT01"]["source_power_labelled_w"] == 100
        assert len(db.history("WT01")) == 1
        assert db.events() == []
    finally:
        db.close()


def test_same_sequence_with_changed_measurement_after_stale_is_rejected(tmp_path):
    db = Historian(tmp_path / "historian.sqlite")
    try:
        db.record(1, reading(), "2026-01-01T00:00:00Z")
        assert db.record(1, reading(), "2026-01-01T00:00:05Z", max_age_seconds=3) is False
        assert db.latest()["WT01"]["quality"] == "STALE"
        with pytest.raises(ValueError, match="sequence"):
            db.record(1, reading(power="999"), "2026-01-01T00:00:06Z", max_age_seconds=3)
        assert db.latest()["WT01"]["quality"] == "STALE"
        assert len(db.history("WT01")) == 1
        assert db.record(1, reading(sequence=2, timestamp="2020-02-05 10:30:00"),
                         "2026-01-01T00:00:07Z", max_age_seconds=3) is True
        assert db.latest()["WT01"]["quality"] == "GOOD"
    finally:
        db.close()


def test_sequence_wrap_with_new_source_tick_remains_supported(tmp_path):
    db = Historian(tmp_path / "historian.sqlite")
    try:
        db.record(1, reading(sequence=65535), "2026-01-01T00:00:00Z")
        assert db.record(1, reading(sequence=1, timestamp="2020-02-05 10:30:00"),
                         "2026-01-01T00:00:01Z") is True
        assert [item["sequence"] for item in db.history("WT01")] == [1, 65535]
    finally:
        db.close()


def test_collector_marks_same_sequence_changed_payload_bad_data(tmp_path, monkeypatch):
    import scripts.scada_collector as collector

    class Response:
        def __init__(self, registers):
            self.registers = registers

        def isError(self):
            return False

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            return True

        async def read_input_registers(self, address, count, device_id):
            power = "999" if device_id == 1 else "100"
            return Response(encode_sample(sample(power=power), 1))

        def close(self):
            pass

    monkeypatch.setattr(collector, "AsyncModbusTcpClient", Client)
    db = Historian(tmp_path / "historian.sqlite")
    try:
        db.record(1, reading(), "2026-01-01T00:00:00Z")
        asyncio.run(poll_once("127.0.0.1", 15020, db))
        assert db.latest()["WT01"]["quality"] == "BAD_DATA"
        assert db.latest()["WT01"]["source_power_labelled_w"] is None
        assert db.history("WT01")[0]["source_power_labelled_w"] == 100
        assert len(db.history("WT01")) == 1
        assert db.events()[0]["kind"] == "BAD_DATA"
        assert db.latest()["WT02"]["quality"] == "GOOD"
    finally:
        db.close()
