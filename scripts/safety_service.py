"""Separate local *simulated* safety process; JSON-lines over a private pipe.

Educational logic only: no certified SIS, physical redundancy, or secure
operator authentication. The pipe is not a network service. A shared host and
plant sensor source remain common-cause failure risks.
"""

import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys

from scripts.simulate import TURBINES, TRIP_SETPOINT_C

ROOT = Path(__file__).resolve().parents[1]
RESET_BELOW_C = 75.0  # simulation-only hysteresis, strictly below


class SafetyLogic:
    """Persistent per-turbine trip latch controlled only by safety samples."""

    def __init__(self):
        self.states = {t: {"latched": False, "reason": None, "last_temp": None} for t in TURBINES}

    def handle(self, message: dict) -> dict:
        turbine_id = message.get("turbine_id")
        if turbine_id not in self.states:
            raise ValueError("unknown turbine ID")
        state = self.states[turbine_id]
        kind = message.get("type")
        result = {"turbine_id": turbine_id, "reset_accepted": False}
        if kind == "sample":
            value = message.get("bearing_temp_c")
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            if not valid:
                state["last_temp"] = None
                if not state["latched"]:
                    state["reason"] = "SENSOR_INVALID"
                state["latched"] = True
            else:
                state["last_temp"] = float(value)
                if value >= TRIP_SETPOINT_C:
                    if not state["latched"]:
                        state["reason"] = "BEARING_OVERTEMP"
                    state["latched"] = True
        elif kind == "reset":
            if (message.get("local_request") is True and state["latched"]
                    and state["last_temp"] is not None and state["last_temp"] < RESET_BELOW_C):
                state["latched"] = False
                state["reason"] = None
                result["reset_accepted"] = True
        else:
            raise ValueError("unknown safety message type")
        result.update({"latched": state["latched"], "reason": state["reason"],
                       "last_temp_c": state["last_temp"], "service_pid": os.getpid()})
        return result


class SafetyClient:
    """Own a private local child process and fail closed if it disappears."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "scripts.safety_service"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )

    def exchange(self, message: dict) -> dict:
        if self.process.poll() is not None:
            raise RuntimeError("safety service unavailable")
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], self.timeout)
            if not ready:
                raise RuntimeError("safety service timeout")
            response = self.process.stdout.readline()
            if not response:
                raise RuntimeError("safety service disconnected")
            result = json.loads(response)
            if "error" in result or not isinstance(result.get("latched"), bool):
                raise RuntimeError("safety service invalid response")
            return result
        except (BrokenPipeError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("safety service communication failed") from exc

    def sample(self, turbine_id: str, temperature: float | None) -> dict:
        return self.exchange({"type": "sample", "turbine_id": turbine_id,
                              "bearing_temp_c": temperature})

    def reset(self, turbine_id: str, local_request: bool = False) -> dict:
        return self.exchange({"type": "reset", "turbine_id": turbine_id,
                              "local_request": local_request})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def main() -> None:
    logic = SafetyLogic()
    for line in sys.stdin:
        try:
            result = logic.handle(json.loads(line))
        except (ValueError, TypeError, AttributeError) as exc:
            result = {"error": str(exc)}
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
