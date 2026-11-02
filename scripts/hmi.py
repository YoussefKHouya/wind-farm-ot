"""Read-only operator HMI; serves only historian observations and source events.

No direct plant truth, safety reset, control commands, or synthetic attack button.
Bind to loopback by default; this is a local interface, not authenticated OT.
"""
import argparse

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

from scripts.ot_gateway import IDS
from scripts.scada_collector import DB_DEFAULT, Historian
from scripts.simulate import ROOT

HTML = ROOT / "web/control_room.html"



def create_app(db_path: Path = DB_DEFAULT) -> FastAPI:
    app = FastAPI(title="Wind-farm historical SCADA replay", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def home():
        return HTML.read_text(encoding="utf-8")

    @app.get("/api/fleet")
    def fleet():
        db = Historian(db_path)
        try:
            return {"mode": "historical-recorded-scada-replay", "turbines": db.latest(), "events": db.events()}
        finally:
            db.close()

    @app.get("/api/history/{turbine_id}")
    def history(turbine_id: str):
        if turbine_id not in IDS.values():
            raise HTTPException(status_code=404, detail="unknown turbine")
        db = Historian(db_path)
        try:
            return {"turbine_id": turbine_id, "observations": db.history(turbine_id)}
        finally:
            db.close()

    @app.get("/api/source-events")
    def source_events():
        db = Historian(db_path)
        try:
            return db.recorded_events()
        finally:
            db.close()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
