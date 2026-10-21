"""Protocol + historian + HMI contract; network smoke also exercised by CLI."""
import asyncio
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import select
import socket
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from scripts.ot_protocol import encode_sample, decode_registers, WORDS
from scripts.scada_collector import Historian, poll_once, DB_DEFAULT, LOGS_DEFAULT, DEFAULT_MAX_AGE_SECONDS
from scripts.hmi import create_app
from scripts.ot_gateway import load_source, DEFAULT_SOURCE
from pymodbus.client import AsyncModbusTcpClient


def sample(turbine="WT01", temp="63", power="-17"):
    return {"source_turbine_id": turbine, "timestamp_source": "2020-02-05 10:20:00",
            "Ambient WindSpeed Avg. [m/s]": "13.5", "Generator Bearing Temp. Avg. [°C]": temp,
            "Rotor RPM Avg. [RPM]": "10.4", "Grid Production Power Avg. [W]": power,
            "missing_data": "False"}


def test_register_contract_roundtrips_signed_power_and_source_timestamp():
    registers = encode_sample(sample(), sequence=7)
    assert len(registers) == WORDS and all(0 <= n <= 65535 for n in registers)
    result = decode_registers(registers)
    assert result["sequence"] == 7 and result["source_timestamp"] == "2020-02-05 10:20:00"
    assert result["wind_mps"] == 13.5 and result["bearing_temp_c"] == 63.0
    assert result["rotor_rpm"] == 10.4 and result["source_power_labelled_w"] == -17.0
    assert result["quality"] == "GOOD"


def test_missing_or_invalid_source_value_is_not_reported_as_zero():
    data = sample(temp="", power="")
    data["missing_data"] = "True"
    result = decode_registers(encode_sample(data, sequence=1))
    assert result["bearing_temp_c"] is None and result["source_power_labelled_w"] is None
    assert result["quality"] == "SOURCE_MISSING"


def test_bad_register_version_and_invalid_timestamp_fail_closed():
    registers = encode_sample(sample(), sequence=1)
    registers[0] = 999
    with pytest.raises(ValueError, match="version"):
        decode_registers(registers)
    with pytest.raises(ValueError, match="source timestamp"):
        encode_sample({**sample(), "timestamp_source": "2020-02-05T10:20:00Z"}, sequence=1)


def test_historian_keeps_observations_and_marks_loss_not_zero(tmp_path):
    db = Historian(tmp_path / "hist.sqlite")
    one = decode_registers(encode_sample(sample(), sequence=1))
    assert db.record(1, one, "2026-01-01T00:00:00Z") is True
    assert db.record(1, one, "2026-01-01T00:00:01Z") is False  # no duplicate tick
    assert db.latest()["WT01"]["source_power_labelled_w"] == -17
    db.mark_unavailable(1, "2026-01-01T00:00:03Z", "COMM_LOSS")
    latest = db.latest()["WT01"]
    assert latest["quality"] == "COMM_LOSS" and latest["source_power_labelled_w"] is None
    assert db.history("WT01")[0]["source_power_labelled_w"] == -17
    assert db.events()[0]["kind"] == "COMM_LOSS"
    db.close()


def test_unchanged_register_sequence_turns_stale_and_needs_new_tick_for_recovery(tmp_path):
    db = Historian(tmp_path / "hist.sqlite")
    one = decode_registers(encode_sample(sample(), sequence=1))
    two = decode_registers(encode_sample({**sample(), "timestamp_source": "2020-02-05 10:30:00"}, sequence=2))
    db.record(1, one, "2026-01-01T00:00:00Z")
    assert db.record(1, one, "2026-01-01T00:00:05Z", max_age_seconds=3) is False
    assert db.latest()["WT01"]["quality"] == "STALE"
    assert db.latest()["WT01"]["source_power_labelled_w"] is None
    assert db.history("WT01")[0]["source_power_labelled_w"] == -17
    db.record(1, one, "2026-01-01T00:00:06Z", max_age_seconds=3)
    assert db.latest()["WT01"]["quality"] == "STALE"
    db.record(1, two, "2026-01-01T00:00:07Z", max_age_seconds=3)
    assert db.latest()["WT01"]["quality"] == "GOOD"
    assert [x["kind"] for x in db.events()] == ["COMM_RECOVERED", "STALE"]
    db.close()


def test_hmi_exposes_historian_view_only_not_protection_reset(tmp_path):
    db_path = tmp_path / "hist.sqlite"
    db = Historian(db_path)
    db.record(1, decode_registers(encode_sample(sample(), sequence=1)), "2026-01-01T00:00:00Z")
    db.close()
    client = TestClient(create_app(db_path))
    response = client.get("/api/fleet")
    assert response.status_code == 200
    assert response.json()["turbines"]["WT01"]["source_power_labelled_w"] == -17
    assert client.get("/").status_code == 200
    assert "Wind Farm Control Room" in client.get("/").text
    assert client.post("/api/reset", json={"turbine_id": "WT01"}).status_code == 404
    assert client.get("/api/history/WT99").status_code == 404


def test_real_modbus_tcp_three_devices_readonly_collector_outage_and_hmi(tmp_path):
    source = Path(__file__).resolve().parents[1] / "data/raw/greek_scada_3t_2020-02-05.csv"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-u", "-m", "scripts.ot_gateway",
                             "--source", str(source), "--port", str(port),
                             "--interval", "0.25", "--limit", "2"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 8)
        assert ready, f"gateway exited {proc.poll()}"
        assert "MODBUS_READY" in proc.stdout.readline()

        async def probe():
            client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0)
            assert await client.connect()
            try:
                readings = []
                for device_id in (1, 2, 3):
                    response = await client.read_input_registers(0, count=WORDS, device_id=device_id)
                    assert not response.isError()
                    readings.append(decode_registers(response.registers))
                rejected = await client.write_register(2, 5000, device_id=1)
                assert rejected.isError()
                followup = await client.read_input_registers(0, count=WORDS, device_id=1)
                assert decode_registers(followup.registers)["wind_mps"] == readings[0]["wind_mps"]
                return readings
            finally:
                client.close()

        readings = asyncio.run(probe())
        assert {r["source_timestamp"] for r in readings} == {"2020-02-05 00:00:00"}
        db = Historian(tmp_path / "historic.sqlite")
        asyncio.run(poll_once("127.0.0.1", port, db))
        assert len(db.latest()) == 3
        assert all(x["quality"] == "GOOD" for x in db.latest().values())
        response = TestClient(create_app(tmp_path / "historic.sqlite")).get("/api/fleet")
        assert response.json()["turbines"]["WT01"]["source_timestamp"] == "2020-02-05 00:00:00"
        proc.terminate()
        proc.wait(timeout=5)
        asyncio.run(poll_once("127.0.0.1", port, db))
        assert all(x["quality"] == "COMM_LOSS" and x["source_power_labelled_w"] is None
                   for x in db.latest().values())
        assert len(db.history("WT01")) == 1
        db.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def test_jump_to_recorded_high_wind_alarm_without_inventing_event(tmp_path):
    source = Path(__file__).resolve().parents[1] / "data/raw/greek_scada_3t_2020-02-05.csv"
    log_source = Path(__file__).resolve().parents[1] / "data/raw/greek_logs_3t_2020-02-05.csv"
    grouped = load_source(source, limit=4, start=62)
    assert grouped[2][0]["timestamp_source"] == "2020-02-05 10:20:00"
    assert grouped[2][2]["timestamp_source"] == "2020-02-05 10:40:00"
    db_path = tmp_path / "historian.sqlite"
    db = Historian(db_path)
    assert db.import_source_events(log_source) == 943
    assert db.import_source_events(log_source) == 0
    for n in (1, 2, 3):
        db.record(n, decode_registers(encode_sample(grouped[n][2], sequence=3)), "2026-01-01T00:00:00Z")
    db.close()
    payload = TestClient(create_app(db_path)).get("/api/source-events").json()
    events = payload["events"]
    assert any(e["source_turbine_id"] == "WT02" and e["Code"] == "144"
               and e["Detected"] == "2020-02-05 10:32:00" for e in events)
    assert any(e["Code"] == "144" for e in payload["alarms"])


def test_full_week_replay_has_unique_source_times_without_short_loop():
    path = Path(__file__).resolve().parents[1] / "data/raw/greek_scada_3t_2020-02-05_11.csv"
    assert DEFAULT_SOURCE == path
    assert LOGS_DEFAULT.name == "greek_logs_3t_2020-02-05_11.csv"
    assert DB_DEFAULT.name == "control_room_week.sqlite"
    assert DEFAULT_MAX_AGE_SECONDS > 10
    groups = load_source(path)
    assert len(groups[1]) == len(groups[2]) == len(groups[3]) == 1008
    assert groups[1][0]["timestamp_source"] == "2020-02-05 00:00:00"
    assert groups[1][-1]["timestamp_source"] == "2020-02-11 23:50:00"
    assert len({x["timestamp_source"] for x in groups[1]}) == len(groups[1])


def test_control_room_separates_source_clock_poll_clock_and_readonly_mode(tmp_path):
    page = TestClient(create_app(tmp_path / "historian.sqlite")).get("/").text
    for marker in ("id=\"site-overview\"", "id=\"turbine-table\"", "id=\"source-alarms\"",
                   "id=\"communications\"", "id=\"trend\"", "id=\"playback-state\""):
        assert marker in page
    assert "historical replay" in page.lower()
    assert "read-only" in page.lower()
    assert "source_timestamp" in page and "observed_at_utc" in page
    assert "POST /api/reset" not in page
