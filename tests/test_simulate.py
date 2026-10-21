from scripts.simulate import load_weather, simulate, write_output


def weather():
    return [
        {"timestamp_utc": "2024-01-01T00:00:00Z", "wind_speed_100m_mps": 10.0, "ambient_temperature_2m_c": 20.0},
        {"timestamp_utc": "2024-01-01T01:00:00Z", "wind_speed_100m_mps": 10.0, "ambient_temperature_2m_c": 20.0},
        {"timestamp_utc": "2024-01-01T02:00:00Z", "wind_speed_100m_mps": 10.0, "ambient_temperature_2m_c": 20.0},
    ]


def test_three_turbines_produce_bounded_power_from_curve():
    rows = simulate(weather())
    assert len(rows) == 9
    assert {r["turbine_id"] for r in rows} == {"T01", "T02", "T03"}
    assert all(0 <= r["actual_power_kw"] <= 2000 for r in rows)
    assert all(r["operating_state"] == "RUNNING" for r in rows)
    assert rows[0]["actual_power_kw"] != rows[1]["actual_power_kw"]


def test_overheat_trips_only_target_and_latches():
    rows = simulate(weather(), overheat_turbine="T02", overheat_at="2024-01-01T01:00:00Z")
    t2 = [r for r in rows if r["turbine_id"] == "T02"]
    assert t2[0]["operating_state"] == "RUNNING"
    assert t2[1]["operating_state"] == "SAFETY_TRIPPED"
    assert t2[2]["operating_state"] == "SAFETY_TRIPPED"
    assert all(r["actual_power_kw"] == 0 and r["rotor_rpm"] == 0 for r in t2[1:])
    assert all(r["operating_state"] == "RUNNING" for r in rows if r["turbine_id"] != "T02")


def test_cutout_wind_produces_zero_without_false_safety_trip():
    sample = [{"timestamp_utc": "2024-01-01T00:00:00Z", "wind_speed_100m_mps": 30.0, "ambient_temperature_2m_c": 20.0}]
    rows = simulate(sample)
    assert all(r["actual_power_kw"] == 0 and r["operating_state"] == "HIGH_WIND_CUTOUT" for r in rows)
    assert all(r["safety_trip"] is False for r in rows)


def test_invalid_overheat_turbine_is_rejected():
    import pytest
    with pytest.raises(ValueError, match="unknown turbine"):
        simulate(weather(), overheat_turbine="T99", overheat_at="2024-01-01T00:00:00Z")


def test_load_weather_rejects_negative_wind(tmp_path):
    import pytest
    csv_path = tmp_path / "weather.csv"
    csv_path.write_text("timestamp_utc,wind_speed_100m_mps,ambient_temperature_2m_c\n2024-01-01T00:00:00Z,-1,12\n")
    with pytest.raises(ValueError, match="wind speed"):
        load_weather(csv_path)


def test_simulated_csv_uses_lf_line_endings(tmp_path):
    output = tmp_path / "simulated.csv"
    write_output(simulate(weather()), output)
    assert b"\r" not in output.read_bytes()
