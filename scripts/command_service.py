"""Level 2 command service: authenticated operator commands to the PLC, with interlocks and audit.

HTTP API (default 127.0.0.1:18081; configurable host for the VM deployment):

  POST /api/commands  {"action": "START"|"STOP", "target": "T01".."T03"}
                      {"action": "OPEN"|"CLOSE", "target": "CB-T01".."CB-T03"|"CB-FDR"}
  POST /api/locks     {"target": "T01".."T03"|"FDR", "locked": true|false, "note": "..."}   supervisor
  GET  /api/locks, GET /api/audit?limit=N, GET /api/whoami

Rules (agreed for the project):
* Operators may START/STOP turbines and OPEN/CLOSE breakers; supervisors may also set
  maintenance locks.
* Before each write the PLC is read fresh. START is refused while the turbine's safety trip is
  latched or it is under a maintenance lock; CLOSE is refused under a lock, and for a turbine
  breaker while its trip is latched. STOP and OPEN are never blocked by an interlock.
* CLOSE on a breaker whose closer coil is still high but whose contact is open (a trip) sends a
  real 0 -> 1 pulse, held long enough for the PLC to scan both edges.
* LOCAL_RESET is never available remotely; it exists only at the plant's local console.
* Every attempt is audited, including failed logins, wrong roles and invalid requests.

Authentication is HTTP Basic against a users file of salted scrypt hashes (mode 0600). It runs
over plain HTTP on the project network: credentials are exposed to anyone who can sniff that
network, which is one of the weaknesses the attack exercises can demonstrate.
"""
import argparse
import base64
import binascii
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from starlette.concurrency import run_in_threadpool
import uvicorn

from scripts.plant_collector import decode_plc, parse_endpoint
from scripts.plc import COIL_BREAKER, COIL_RUN, N_COILS, N_DI, N_IR
from scripts.simulate import ROOT

TURBINES = ("T01", "T02", "T03")
BREAKERS = {"CB-T01": "T01", "CB-T02": "T02", "CB-T03": "T03", "CB-FDR": "FDR"}
LOCK_TARGETS = (*TURBINES, "FDR")
ROLES = ("operator", "supervisor")
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{1,31}")
MIN_PASSWORD = 8
RUNTIME = ROOT / "data/runtime"
DB_DEFAULT = RUNTIME / "commands.sqlite"
USERS_DEFAULT = RUNTIME / "users.json"
PORT_DEFAULT = 18081


# -- passwords --------------------------------------------------------------------------

def hash_password(password: str, n: int = 2 ** 14, r: int = 8, p: int = 1) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return {"salt": salt.hex(), "hash": digest.hex(), "n": n, "r": r, "p": p}


def verify_password(password: str, record: dict) -> bool:
    try:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]),
                                n=record["n"], r=record["r"], p=record["p"], dklen=32)
        return hmac.compare_digest(digest.hex(), record["hash"])
    except (KeyError, ValueError, TypeError):
        return False


_DUMMY = hash_password("timing-equaliser", n=2 ** 10)


def load_users(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    users = data.get("users", {})
    for name, record in users.items():
        if not USERNAME_RE.fullmatch(name) or record.get("role") not in ROLES:
            raise ValueError(f"invalid user entry {name!r}")
    return users


# -- persistence ------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, actor TEXT, role TEXT, client TEXT,
                action TEXT, target TEXT, accepted INTEGER NOT NULL, reason TEXT,
                writes TEXT NOT NULL, note TEXT);
            CREATE TABLE IF NOT EXISTS locks (
                target TEXT PRIMARY KEY, by TEXT NOT NULL, at_utc TEXT NOT NULL, note TEXT);
        """)
        self.conn.commit()
        self.lock = threading.Lock()

    def audit(self, *, actor, role, client, action, target, accepted, reason, writes=(), note=None):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO audit (at_utc, actor, role, client, action, target, accepted, reason, writes, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), actor, role, client, action, target, int(accepted), reason, json.dumps(list(writes)), note))

    def entries(self, limit: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, at_utc, actor, role, client, action, target, accepted, reason, "
                                     "writes, note FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "at_utc", "actor", "role", "client", "action", "target", "accepted", "reason", "writes", "note")
        result = []
        for row in rows:
            entry = dict(zip(keys, row))
            entry["accepted"] = bool(entry["accepted"])
            entry["writes"] = json.loads(entry["writes"])
            result.append(entry)
        return result

    def locks(self) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT target, by, at_utc, note FROM locks").fetchall()
        return {t: {"by": by, "at_utc": at, "note": note} for t, by, at, note in rows}

    def set_lock(self, target, locked, by, note):
        with self.lock, self.conn:
            if locked:
                self.conn.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?,?)", (target, by, _now(), note))
            else:
                self.conn.execute("DELETE FROM locks WHERE target=?", (target,))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -- PLC access -------------------------------------------------------------------------

class PlcUnavailable(Exception):
    pass


class Plc:
    def __init__(self, endpoint: tuple[str, int]):
        self.endpoint = endpoint

    def _client(self):
        client = ModbusTcpClient(self.endpoint[0], port=self.endpoint[1], timeout=1.0, retries=0)
        if not client.connect():
            raise PlcUnavailable()
        return client

    def read(self) -> dict:
        client = self._client()
        try:
            coils = client.read_coils(0, count=N_COILS, device_id=1)
            di = client.read_discrete_inputs(0, count=N_DI, device_id=1)
            ir = client.read_input_registers(0, count=N_IR, device_id=1)
            if any(r.isError() for r in (coils, di, ir)):
                raise PlcUnavailable()
            return decode_plc(coils.bits, di.bits, ir.registers)
        except (ModbusException, OSError, ValueError) as exc:
            raise PlcUnavailable() from exc
        finally:
            client.close()

    def write(self, coil: int, value: bool):
        client = self._client()
        try:
            if client.write_coil(coil, value, device_id=1).isError():
                raise PlcUnavailable()
        except (ModbusException, OSError) as exc:
            raise PlcUnavailable() from exc
        finally:
            client.close()

    def coil(self, coil: int) -> bool:
        client = self._client()
        try:
            response = client.read_coils(coil, count=1, device_id=1)
            if response.isError():
                raise PlcUnavailable()
            return bool(response.bits[0])
        except (ModbusException, OSError) as exc:
            raise PlcUnavailable() from exc
        finally:
            client.close()


# -- application ------------------------------------------------------------------------

class Refusal(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _validate(body) -> tuple[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("action"), str) or not isinstance(body.get("target"), str):
        raise Refusal(400, "INVALID_COMMAND")
    action, target = body["action"], body["target"]
    if action == "LOCAL_RESET":
        raise Refusal(400, "LOCAL_CONSOLE_ONLY")
    if (action in ("START", "STOP") and target in TURBINES) or (action in ("OPEN", "CLOSE") and target in BREAKERS):
        return action, target
    raise Refusal(400, "INVALID_COMMAND")


def create_app(db_path: Path = DB_DEFAULT, users_path: Path = USERS_DEFAULT,
               plc: tuple[str, int] = ("127.0.0.1", 15021), pulse_seconds: float = 0.5) -> FastAPI:
    users = load_users(users_path)
    store = Store(db_path)
    controller = Plc(plc)
    command_lock = threading.Lock()  # read-check-write is atomic across operators
    app = FastAPI(title="Wind farm command service", docs_url=None, redoc_url=None, openapi_url=None)

    def client_of(request):
        return request.client.host if request.client else None

    def authenticate(request: Request, action, target):
        header = request.headers.get("authorization", "")
        name = None
        if header.lower().startswith("basic "):
            try:
                name, _, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                name, password = None, ""
            record = users.get(name) if name else None
            if record and verify_password(password, record):
                return name, record["role"]
            if not record:
                verify_password(password, _DUMMY)
        store.audit(actor=name or None, role=None, client=client_of(request), action=action, target=target,
                    accepted=False, reason="AUTH_FAILED")
        raise Refusal(401, "AUTH_FAILED")

    def respond(refusal: Refusal):
        headers = {"WWW-Authenticate": 'Basic realm="wind-farm-ot"'} if refusal.status == 401 else None
        return JSONResponse({"accepted": False, "reason": refusal.reason}, status_code=refusal.status, headers=headers)

    def execute(actor, role, client, action, target):
        writes: list[dict] = []
        with command_lock:
            try:
                state = controller.read()
                if not state["field_link"]:
                    raise Refusal(503, "FIELD_LINK_LOST")
                locks = store.locks()
                if action in ("START", "STOP"):
                    coil = COIL_RUN[target]
                    if action == "START":
                        if target in locks:
                            raise Refusal(409, "MAINTENANCE_LOCK")
                        if state["safety_latched"][target]:
                            raise Refusal(409, "SAFETY_TRIP_LATCHED")
                    plan = [True] if action == "START" else [False]
                else:
                    unit = BREAKERS[target]
                    coil = COIL_BREAKER[unit]
                    if action == "CLOSE":
                        if unit in locks:
                            raise Refusal(409, "MAINTENANCE_LOCK")
                        if unit in TURBINES and state["safety_latched"][unit]:
                            raise Refusal(409, "SAFETY_TRIP_LATCHED")
                        tripped = state["breaker_command"][unit] and not state["breaker_closed"][unit]
                        plan = [False, True] if tripped else [True]
                    else:
                        plan = [False]
                for index, value in enumerate(plan):
                    if index:
                        time.sleep(pulse_seconds)
                    controller.write(coil, value)
                    writes.append({"coil": coil, "value": value})
                if controller.coil(coil) != plan[-1]:
                    raise Refusal(409, "PLC_REJECTED")
            except PlcUnavailable:
                refusal = Refusal(503, "PLC_UNAVAILABLE")
                store.audit(actor=actor, role=role, client=client, action=action, target=target,
                            accepted=False, reason=refusal.reason, writes=writes)
                raise refusal
            except Refusal as refusal:
                store.audit(actor=actor, role=role, client=client, action=action, target=target,
                            accepted=False, reason=refusal.reason, writes=writes)
                raise
        store.audit(actor=actor, role=role, client=client, action=action, target=target,
                    accepted=True, reason=None, writes=writes)
        return {"accepted": True, "reason": None, "action": action, "target": target, "writes": writes}

    async def body_of(request):
        try:
            return json.loads(await request.body())
        except ValueError:
            return None

    @app.post("/api/commands")
    async def commands(request: Request):
        body = await body_of(request)
        action = body.get("action") if isinstance(body, dict) and isinstance(body.get("action"), str) else None
        target = body.get("target") if isinstance(body, dict) and isinstance(body.get("target"), str) else None
        try:
            actor, role = authenticate(request, action, target)
            try:
                action, target = _validate(body)
            except Refusal as refusal:
                store.audit(actor=actor, role=role, client=client_of(request), action=action, target=target,
                            accepted=False, reason=refusal.reason)
                raise
            return await run_in_threadpool(execute, actor, role, client_of(request), action, target)
        except Refusal as refusal:
            return respond(refusal)

    @app.post("/api/locks")
    async def set_lock(request: Request):
        body = await body_of(request)
        target = body.get("target") if isinstance(body, dict) and isinstance(body.get("target"), str) else None
        try:
            actor, role = authenticate(request, "LOCK", target)
            refusal = None
            if role != "supervisor":
                refusal = Refusal(403, "ROLE_NOT_PERMITTED")
            elif target not in LOCK_TARGETS or not isinstance(body.get("locked"), bool) \
                    or not isinstance(body.get("note", ""), str):
                refusal = Refusal(400, "INVALID_COMMAND")
            action = "LOCK" if isinstance(body, dict) and body.get("locked") is not False else "UNLOCK"
            note = body.get("note") if isinstance(body, dict) and isinstance(body.get("note"), str) else None
            if refusal:
                store.audit(actor=actor, role=role, client=client_of(request), action=action, target=target,
                            accepted=False, reason=refusal.reason, note=note)
                raise refusal
            store.set_lock(target, body["locked"], actor, note)
            store.audit(actor=actor, role=role, client=client_of(request), action=action, target=target,
                        accepted=True, reason=None, note=note)
            return {"accepted": True, "target": target, "locked": body["locked"]}
        except Refusal as refusal:
            return respond(refusal)

    @app.get("/api/locks")
    async def get_locks(request: Request):
        try:
            authenticate(request, "READ_LOCKS", None)
            return {"locks": store.locks()}
        except Refusal as refusal:
            return respond(refusal)

    @app.get("/api/audit")
    async def get_audit(request: Request, limit: int = 100):
        try:
            authenticate(request, "READ_AUDIT", None)
            return {"entries": store.entries(max(1, min(limit, 1000)))}
        except Refusal as refusal:
            return respond(refusal)

    @app.get("/api/whoami")
    async def whoami(request: Request):
        try:
            actor, role = authenticate(request, "WHOAMI", None)
            return {"user": actor, "role": role}
        except Refusal as refusal:
            return respond(refusal)

    return app


# -- CLI --------------------------------------------------------------------------------

def add_user(args, parser):
    if not USERNAME_RE.fullmatch(args.name):
        parser.error("username: lowercase letters, digits, . _ - (2-32 chars)")
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD:
        parser.error(f"password must be at least {MIN_PASSWORD} characters")
    data = {"users": {}}
    if args.users.exists():
        data = json.loads(args.users.read_text(encoding="utf-8"))
    data["users"][args.name] = {"role": args.role, **hash_password(password)}
    args.users.parent.mkdir(parents=True, exist_ok=True)
    temp = args.users.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(temp, 0o600)
    os.replace(temp, args.users)
    print(f"user {args.name} saved with role {args.role} in {args.users}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the command service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=PORT_DEFAULT)
    serve.add_argument("--plc", type=parse_endpoint, default=("127.0.0.1", 15021), help="PLC host:port")
    serve.add_argument("--db", type=Path, default=DB_DEFAULT)
    serve.add_argument("--users", type=Path, default=USERS_DEFAULT)
    serve.add_argument("--pulse", type=float, default=0.5, help="closer pulse width in seconds")
    user = sub.add_parser("add-user", help="create or update a user (password is hashed)")
    user.add_argument("name")
    user.add_argument("--role", choices=ROLES, required=True)
    user.add_argument("--users", type=Path, default=USERS_DEFAULT)
    user.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = parser.parse_args()
    if args.command == "add-user":
        add_user(args, parser)
        return
    if not math.isfinite(args.pulse) or args.pulse <= 0:
        parser.error("pulse must be positive")
    if not args.users.exists():
        parser.error(f"no users file at {args.users}; create one with add-user")
    app = create_app(args.db, args.users, args.plc, args.pulse)
    print(f"COMMANDS_READY http://{args.host}:{args.port} plc={args.plc[0]}:{args.plc[1]}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
