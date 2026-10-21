import pytest

from scripts.fetch_weather import extract_weather, write_weather


def payload():
    return {
        "latitude": 27.94376,
        "longitude": -12.886139,
        "hourly_units": {"time": "iso8601", "wind_speed_100m": "m/s", "temperature_2m": "°C"},
        "hourly": {
            "time": ["2024-01-01T00:00", "2024-01-01T01:00"],
            "wind_speed_100m": [3.96, 5.1],
            "temperature_2m": [14.3, 14.1],
        },
    }


def test_extract_weather_preserves_source_measurements_and_utc():
    rows = extract_weather(payload())
    assert rows == [
        {"timestamp_utc": "2024-01-01T00:00:00Z", "wind_speed_100m_mps": 3.96, "ambient_temperature_2m_c": 14.3},
        {"timestamp_utc": "2024-01-01T01:00:00Z", "wind_speed_100m_mps": 5.1, "ambient_temperature_2m_c": 14.1},
    ]


def test_extract_weather_rejects_missing_weather():
    data = payload()
    data["hourly"]["wind_speed_100m"][1] = None
    with pytest.raises(ValueError, match="missing or invalid"):
        extract_weather(data)


def test_extract_weather_rejects_wrong_units():
    data = payload()
    data["hourly_units"]["wind_speed_100m"] = "km/h"
    with pytest.raises(ValueError, match="expected m/s"):
        extract_weather(data)


def test_extract_weather_rejects_duplicate_timestamps():
    data = payload()
    data["hourly"]["time"][1] = data["hourly"]["time"][0]
    with pytest.raises(ValueError, match="strictly increasing"):
        extract_weather(data)


def test_weather_csv_uses_lf_line_endings(tmp_path):
    output = tmp_path / "weather.csv"
    write_weather(extract_weather(payload()), output)
    assert b"\r" not in output.read_bytes()
