"""Download real historical *reanalysis* weather for the town of Tarfaya.

This is a modelled historical grid point near Tarfaya, not measured turbine-site
telemetry and not data from any wind-farm operator.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
LATITUDE = 27.9392  # Open-Meteo geocoding result for the *town* of Tarfaya
LONGITUDE = -12.92604
FIELDS = ("timestamp_utc", "wind_speed_100m_mps", "ambient_temperature_2m_c")


def extract_weather(payload: dict) -> list[dict]:
    """Validate source units and turn hourly UTC data into CSV-ready rows."""
    hourly = payload.get("hourly", {})
    units = payload.get("hourly_units", {})
    if units.get("wind_speed_100m") != "m/s":
        raise ValueError("expected m/s for wind_speed_100m")
    if units.get("temperature_2m") != "°C":
        raise ValueError("expected °C for temperature_2m")
    timestamps = hourly.get("time", [])
    winds = hourly.get("wind_speed_100m", [])
    temps = hourly.get("temperature_2m", [])
    if not timestamps or len(timestamps) != len(winds) or len(winds) != len(temps):
        raise ValueError("missing or uneven hourly weather series")
    rows = []
    previous = None
    for timestamp, wind, temp in zip(timestamps, winds, temps, strict=True):
        try:
            dt = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
            speed = float(wind)
            temperature = float(temp)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("missing or invalid hourly weather measurement") from exc
        if not math.isfinite(speed) or speed < 0 or not math.isfinite(temperature):
            raise ValueError("missing or invalid hourly weather measurement")
        if previous is not None and dt <= previous:
            raise ValueError("timestamps must be strictly increasing")
        previous = dt
        rows.append({
            "timestamp_utc": dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "wind_speed_100m_mps": speed,
            "ambient_temperature_2m_c": temperature,
        })
    return rows


def write_weather(rows: list[dict], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fetch(start: str, end: str, csv_path: Path, raw_path: Path) -> tuple[int, str]:
    """Download and persist the actual API response plus validated CSV."""
    if datetime.fromisoformat(start) > datetime.fromisoformat(end):
        raise ValueError("start date is after end date")
    url = BASE_URL + "?" + urlencode({
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": start,
        "end_date": end,
        "hourly": "wind_speed_100m,temperature_2m",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    })
    with urlopen(url, timeout=90) as response:
        payload = json.load(response)
    if payload.get("error"):
        raise ValueError(f"source API error: {payload.get('reason', 'unknown')}")
    rows = extract_weather(payload)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({"request_url": url, "response": payload}, indent=2) + "\n", encoding="utf-8")
    write_weather(rows, csv_path)
    return len(rows), url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-01-07")
    parser.add_argument("--output", type=Path, default=ROOT / "data/raw/tarfaya_weather_2024-01-01_07.csv")
    parser.add_argument("--raw-json", type=Path, default=ROOT / "data/raw/tarfaya_open_meteo_response.json")
    args = parser.parse_args()
    count, url = fetch(args.start, args.end, args.output, args.raw_json)
    print(f"Saved {count} historical reanalysis rows to {args.output}")
    print(f"Raw response: {args.raw_json}")
    print(f"Source: {url}")


if __name__ == "__main__":
    main()
