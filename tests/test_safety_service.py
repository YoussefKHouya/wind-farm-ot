import os

import pytest

from scripts.safety_service import SafetyClient, SafetyLogic


def test_high_bearing_temperature_trips_and_stays_latched():
    logic = SafetyLogic()
    assert logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 30})["latched"] is False
    hot = logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 85})
    assert hot["latched"] is True and hot["reason"] == "BEARING_OVERTEMP"
    cooled = logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 35})
    assert cooled["latched"] is True
    assert logic.handle({"type": "sample", "turbine_id": "T01", "bearing_temp_c": 35})["latched"] is False


def test_invalid_sensor_trips_fail_closed():
    logic = SafetyLogic()
    status = logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": None})
    assert status["latched"] is True and status["reason"] == "SENSOR_INVALID"


def test_reset_requires_valid_cool_sensor_and_explicit_local_request():
    logic = SafetyLogic()
    logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 90})
    assert logic.handle({"type": "reset", "turbine_id": "T02"})["reset_accepted"] is False
    logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 80})
    assert logic.handle({"type": "reset", "turbine_id": "T02", "local_request": True})["reset_accepted"] is False
    logic.handle({"type": "sample", "turbine_id": "T02", "bearing_temp_c": 65})
    cleared = logic.handle({"type": "reset", "turbine_id": "T02", "local_request": True})
    assert cleared["reset_accepted"] is True and cleared["latched"] is False


def test_unknown_turbine_is_rejected():
    with pytest.raises(ValueError, match="unknown turbine"):
        SafetyLogic().handle({"type": "sample", "turbine_id": "T99", "bearing_temp_c": 20})


def test_safety_client_is_an_actual_separate_process():
    with SafetyClient() as client:
        result = client.sample("T02", 90)
        assert result["latched"] is True
        assert result["service_pid"] != os.getpid()
        assert client.sample("T02", 30)["latched"] is True
        assert client.reset("T02", local_request=True)["reset_accepted"] is True


def test_dead_safety_service_refuses_to_report_healthy():
    with SafetyClient() as client:
        client.process.kill()
        client.process.wait(timeout=3)
        with pytest.raises(RuntimeError, match="safety service"):
            client.sample("T02", 20)
