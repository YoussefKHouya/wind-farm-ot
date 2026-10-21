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

## Measured wind-farm SCADA track (separate from the Moroccan scenario)

- Original publisher record: Aristotelis Ntafalias, Sofia Visvardi, Axel Weißenfeld, *Six-Month Monitoring Dataset from a 10-Turbine Onshore Wind Farm in Greece* (2024), DOI 10.5281/zenodo.14546480, https://zenodo.org/records/14546480 — published operational SCADA monitoring dataset from **ten turbines at a Greek wind farm**, January–June 2020; licensed **CC BY 4.0** (https://creativecommons.org/licenses/by/4.0/). These CSVs are an attributed, date- and turbine-filtered adaptation of that source; no source measurement values were changed. This is not Tarfaya or a claim of access to live equipment.
- Original XLSX downloaded from https://zenodo.org/api/records/14546480/files/SCADA__monitoring_dataset_2020.xlsx/content to the Git-ignored `data/external/` directory. Source MD5 `a586c4d972bc4804a8de29c75870905e`; local SHA-256 `f5e451b72aa9ee5422cd40ce27a977dc65b17b60aab28c2aa056d82261720426`. Keep this pristine.
- `scripts/extract_scada.py` streams the three source turbine sheets **WT01–WT03** and their native event logs, preserving source column names, units, gaps, recorded negative power values, and timestamps. The displayed power column's `[W]` unit is preserved as published; do **not** silently reinterpret the apparently kW-scale numbers. The workbook does not specify a verified timestamp timezone, so output is `timestamp_source`, not UTC.
- `data/raw/greek_scada_3t_2020-02-05.csv` holds 432 recorded 10-minute measurements (144 per turbine); `data/raw/greek_logs_3t_2020-02-05.csv` holds 943 actual source events, including 146 source-labelled alarms. These are *source observations*; the extraction does not synthesize alarms.
- `scripts/replay_scada.py` feeds recorded **SCADA** bearing temperatures through the separate local safety service and adds `counterfactual_trip`. This is a **hypothetical project policy evaluation**, NOT a recorded protection trip, an independent physical SIS sensor, or any command sent to the Greek wind farm. The 85°C project threshold is not a source/OEM setting; on the selected day it produces **no** overtemperature trips.
- All previously described Tarfaya `data/derived/` CSVs are model outputs. The newly named `greek_scada_safety_counterfactual_2020-02-05.csv` copies authentic measurements and appends explicitly hypothetical safety decisions; do not present the entire file as raw measurements.
