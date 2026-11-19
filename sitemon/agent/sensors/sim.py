"""Simulated sensors for tests and for trying sitemon without hardware."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from typing import Literal

from .base import SensorError, Value

Clock = Callable[[], float]


class SimSensor:
    """A sensor whose values come from a function of time.

    ``values`` maps each key to a function ``f(t) -> value`` where ``t`` is
    seconds since the sensor was created. ``overrides`` pins a key to a fixed
    value (tests use this to force a low battery or an open door).
    """

    def __init__(
        self,
        name: str,
        kind: Literal["reading", "state"],
        values: dict[str, Callable[[float], Value]],
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self.kind: Literal["reading", "state"] = kind
        self._values = values
        self._clock = clock
        self._t0 = clock()
        self.overrides: dict[str, Value] = {}
        self.fail_reads = 0  # the next N reads raise SensorError
        self.fail_forever = False

    def read(self) -> dict[str, Value]:
        if self.fail_forever or self.fail_reads > 0:
            self.fail_reads = max(0, self.fail_reads - 1)
            raise SensorError(f"{self.name}: simulated I2C error")
        t = self._clock() - self._t0
        out = {k: f(t) for k, f in self._values.items()}
        out.update(self.overrides)
        return out

    def close(self) -> None:
        pass


def _noise(rng: random.Random, scale: float) -> float:
    return rng.uniform(-scale, scale)


def battery(
    name: str = "batt",
    *,
    float_v: float = 13.4,
    drift_v_per_day: float = 0.0,
    load_a: float = 2.0,
    seed: int = 1,
    clock: Clock = time.monotonic,
) -> SimSensor:
    """A float-charged battery: small diurnal swing (solar), optional slow decline."""
    rng = random.Random(seed)

    def v(t: float) -> float:
        day = t / 86400
        swing = 0.25 * math.sin(2 * math.pi * day)
        return round(float_v + swing - drift_v_per_day * day + _noise(rng, 0.01), 3)

    def a(t: float) -> float:
        return round(load_a + _noise(rng, 0.05), 3)

    def w(t: float) -> float:
        return round(v(t) * a(t), 2)

    return SimSensor(name, "reading", {"v": v, "a": a, "w": w}, clock=clock)


def environment(
    name: str = "env", *, base_c: float = 22.0, seed: int = 2, clock: Clock = time.monotonic
) -> SimSensor:
    rng = random.Random(seed)

    def temp(t: float) -> float:
        return round(base_c + 6 * math.sin(2 * math.pi * t / 86400) + _noise(rng, 0.1), 2)

    def rh(t: float) -> float:
        return round(45 - 10 * math.sin(2 * math.pi * t / 86400) + _noise(rng, 0.5), 1)

    def hpa(t: float) -> float:
        return round(1013.2 + _noise(rng, 0.2), 1)

    return SimSensor(name, "reading", {"temp_c": temp, "rh": rh, "hpa": hpa}, clock=clock)


def switch(
    name: str, key: str, *, active: bool = False, clock: Clock = time.monotonic
) -> SimSensor:
    """A door or water input that stays where it is put (set ``overrides[key]``)."""
    return SimSensor(name, "state", {key: lambda t: active}, clock=clock)
