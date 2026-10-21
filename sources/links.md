# Sources and provenance

Verified 2026-10-21. All weather values are **historical model/reanalysis output** from Open-Meteo at a grid point near the town of Tarfaya, Morocco, *not measured on a turbine or sourced from a wind-farm operator*.

| Source | Purpose | Details |
|---|---|---|
| https://geocoding-api.open-meteo.com/v1/search?name=Tarfaya&count=5&language=en&format=json | Town reference, not a wind-farm coordinate | Town result: 27.9392, -12.92604; Open-Meteo historical endpoint returned grid cell 27.94376, -12.886139 |
| https://archive-api.open-meteo.com/v1/archive?latitude=27.9392&longitude=-12.92604&start_date=2024-01-01&end_date=2024-01-07&hourly=wind_speed_100m%2Ctemperature_2m&wind_speed_unit=ms&timezone=UTC | Hourly wind at 100 m and temperature at 2 m | 168 UTC hours; m/s and Celsius; exact API response preserved in `data/raw/tarfaya_open_meteo_response.json` |
| https://open-meteo.com/en/docs/historical-weather-api | Data semantics | Historical weather is reanalysis/model output, not site instrumentation |
| https://open-meteo.com/en/license | Attribution | API data offered under CC BY 4.0; retain source/attribution if redistributing CSV |
| https://windpowerlib.readthedocs.io/en/latest/modelchain_example_notebook.html | Power model | `windpowerlib` ModelChain takes weather and turbine curve to compute output |

The CSV `data/raw/tarfaya_weather_2024-01-01_07.csv` contains actual API-returned historical **model data**; `data/derived/*.csv` are **simulated turbine outputs**, not real generation or measurements. `V90/2000` is a representative library curve, not a claim about any operator's equipment. Temperature dynamics, rotor RPM, spatial wind multipliers, injected bearing fault, and 85°C trip threshold are explicitly synthetic.
