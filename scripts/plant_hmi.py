"""Level 2 operator HMI for the simulated wind farm (2D, high-performance style).

Serves web/plant_hmi.html and a small API (default 127.0.0.1:18082):

  GET  /api/overview          latest PLC/RTU samples with quality, collector health, open alarms
  GET  /api/trend?points=N    export and turbine power history (chronological)
  GET  /api/journal           alarm journal
  POST /api/alarms/{id}/ack   acknowledge an alarm (login required)
  POST /api/commands, POST/GET /api/locks, GET /api/audit, GET /api/whoami
                              forwarded unchanged to the command service with the caller's login

The HMI keeps no users of its own: the command service is the single identity source. It checks
every command, and the HMI asks it to verify a login before recording an acknowledgement under
that user's name. Viewing is open without login (typical for a control-room screen, and the
"exposed HMI" weakness in the attack exercises); every control action needs a login. The browser
keeps credentials in memory only. The HMI reads the historians and never talks to a controller.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
import uvicorn

from scripts.alarm_engine import DB_DEFAULT as ALARM_DB_DEFAULT, STALE_AFTER_DEFAULT, AlarmStore
from scripts.plant_collector import DB_DEFAULT as PLANT_DB_DEFAULT, PlantHistorian
from scripts.rtu import TURBINES
from scripts.simulate import ROOT

PAGE = ROOT / "web/plant_hmi.html"
PORT_DEFAULT = 18082
COMMANDS_DEFAULT = "http://127.0.0.1:18081"
NO_DATA = {"quality": "NO_DATA", "observed_at_utc": None, "values": None}


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _forward(base: str, method: str, path: str, body: bytes | None, authorization: str | None):
    request = urllib.request.Request(base + path, data=body, method=method)
    request.add_header("content-type", "application/json")
    if authorization:
        request.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"null")
        except ValueError:
            return error.code, {"accepted": False, "reason": "COMMAND_SERVICE_ERROR"}
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"accepted": False, "reason": "COMMAND_SERVICE_UNAVAILABLE"}


def create_app(plant_db: Path = PLANT_DB_DEFAULT, alarm_db: Path = ALARM_DB_DEFAULT,
               commands_url: str = COMMANDS_DEFAULT, stale_after: float = STALE_AFTER_DEFAULT) -> FastAPI:
    app = FastAPI(title="Wind farm operator HMI", docs_url=None, redoc_url=None, openapi_url=None)
    commands_url = commands_url.rstrip("/")

    def read_plant():
        plant = PlantHistorian(plant_db)
        try:
            return plant.latest(), plant.last_heartbeat(), plant
        except Exception:
            plant.close()
            raise

    @app.get("/", response_class=HTMLResponse)
    def home():
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/overview")
    def overview():
        latest, heartbeat, plant = read_plant()
        plant.close()
        store = AlarmStore(alarm_db)
        try:
            alarms = store.open_alarms()
        finally:
            store.close()
        age = None if heartbeat is None else max(0.0, time.time() - heartbeat)
        return {"mode": "simulated-plant-operations", "now_utc": _now_text(),
                "collector": {"heartbeat_age_s": age, "ok": age is not None and age <= stale_after},
                "plc": latest.get("plc", NO_DATA), "rtu": latest.get("rtu", NO_DATA), "alarms": alarms}

    @app.get("/api/trend")
    def trend(points: int = 120):
        _, _, plant = read_plant()
        try:
            history = plant.history("rtu", limit=max(2, min(points, 2000)))
        finally:
            plant.close()
        return {"points": [{"t": sample["observed_at_utc"], "export_kw": sample["values"]["farm"]["export_kw"],
                            "turbines": {t: sample["values"]["turbines"][t]["power_kw"] for t in TURBINES}}
                           for sample in reversed(history)]}

    @app.get("/api/journal")
    def journal(limit: int = 50):
        store = AlarmStore(alarm_db)
        try:
            return {"events": store.journal(max(1, min(limit, 500)))}
        finally:
            store.close()

    @app.post("/api/alarms/{alarm_id}/ack")
    async def acknowledge(alarm_id: int, request: Request):
        authorization = request.headers.get("authorization")
        if not authorization:
            return JSONResponse({"acked": False, "reason": "AUTH_FAILED"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="wind-farm-ot"'})
        status, identity = await run_in_threadpool(_forward, commands_url, "GET", "/api/whoami", None, authorization)
        if status != 200 or not isinstance(identity, dict) or "user" not in identity:
            reason = (identity or {}).get("reason", "AUTH_FAILED") if isinstance(identity, dict) else "AUTH_FAILED"
            return JSONResponse({"acked": False, "reason": reason}, status_code=status if status != 200 else 401)
        store = AlarmStore(alarm_db)
        try:
            acked = store.acknowledge(alarm_id, identity["user"], _now_text())
        finally:
            store.close()
        if not acked:
            return JSONResponse({"acked": False, "reason": "NOT_ACKNOWLEDGEABLE"}, status_code=409)
        return {"acked": True, "alarm_id": alarm_id, "by": identity["user"]}

    async def proxy(request: Request, method: str, path: str):
        body = await request.body() if method == "POST" else None
        query = f"?{request.url.query}" if request.url.query else ""
        status, payload = await run_in_threadpool(_forward, commands_url, method, path + query, body,
                                                  request.headers.get("authorization"))
        return JSONResponse(payload, status_code=status)

    @app.post("/api/commands")
    async def commands(request: Request):
        return await proxy(request, "POST", "/api/commands")

    @app.post("/api/locks")
    async def set_lock(request: Request):
        return await proxy(request, "POST", "/api/locks")

    @app.get("/api/locks")
    async def get_locks(request: Request):
        return await proxy(request, "GET", "/api/locks")

    @app.get("/api/audit")
    async def audit(request: Request):
        return await proxy(request, "GET", "/api/audit")

    @app.get("/api/whoami")
    async def whoami(request: Request):
        return await proxy(request, "GET", "/api/whoami")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT_DEFAULT)
    parser.add_argument("--plant-db", type=Path, default=PLANT_DB_DEFAULT)
    parser.add_argument("--alarm-db", type=Path, default=ALARM_DB_DEFAULT)
    parser.add_argument("--commands", default=COMMANDS_DEFAULT, help="command service base URL")
    args = parser.parse_args()
    print(f"HMI_READY http://{args.host}:{args.port} commands={args.commands}", flush=True)
    uvicorn.run(create_app(args.plant_db, args.alarm_db, args.commands), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
