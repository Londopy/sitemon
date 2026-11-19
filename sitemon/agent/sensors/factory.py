"""Build sensors from ``[[sensors]]`` config entries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ...config import ConfigError, SensorConfig
from .base import Sensor, SensorError, Value


class Deferred:
    """Constructs a hardware sensor on first use, and again after it fails to.

    A chip that doesn't answer at boot (loose connector, RF on the bus, a
    power rail still coming up) must not stop the agent: it becomes a sensor
    fault the hub alerts on, and the agent keeps trying.
    """

    def __init__(
        self, name: str, kind: Literal["reading", "state"], ctor: Callable[[], Sensor]
    ) -> None:
        self.name = name
        self.kind: Literal["reading", "state"] = kind
        self._ctor = ctor
        self._inner: Sensor | None = None

    def read(self) -> dict[str, Value]:
        if self._inner is None:
            try:
                self._inner = self._ctor()
            except (ImportError, OSError, ValueError) as e:  # no smbus2, no /dev/i2c-N
                raise SensorError(f"{self.name}: cannot open: {e}") from e
        return self._inner.read()

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()


def _ctor(
    cls: Any, name: str, bus: Callable[[int], Any], n: int, *args: Any
) -> Callable[[], Sensor]:
    # Binds the arguments now; the bus is opened only when the sensor is first read.
    def make() -> Sensor:
        sensor: Sensor = cls(name, bus(n), *args)
        return sensor

    return make


def _int(opts: dict[str, Any], key: str, default: int | None = None) -> int:
    if key not in opts:
        if default is None:
            raise ConfigError(f"missing {key}")
        return default
    return int(opts[key])


def build_sensors(configs: list[SensorConfig]) -> list[Sensor]:
    """Hardware drivers import smbus2/gpiozero only here, so a laptop running
    simulated sensors never needs them."""
    from . import sim

    buses: dict[int, Any] = {}
    out: list[Sensor] = []

    def bus(n: int) -> Any:
        from .i2c import I2CBus

        if n not in buses:
            buses[n] = I2CBus(n)
        return buses[n]

    for sc in configs:
        o = sc.options
        where = f"sensor {sc.name!r} ({sc.type})"
        try:
            if sc.type in ("ina226", "ina219"):
                from .ina import INA219, INA226

                cls = INA226 if sc.type == "ina226" else INA219
                if "shunt_ohms" not in o:
                    raise ConfigError("missing shunt_ohms")
                n, addr, ohms = _int(o, "bus", 1), _int(o, "address", 0x40), float(o["shunt_ohms"])
                out.append(Deferred(sc.name, "reading", _ctor(cls, sc.name, bus, n, addr, ohms)))
            elif sc.type == "bme280":
                from .bme280 import BME280

                n, addr = _int(o, "bus", 1), _int(o, "address", 0x76)
                out.append(Deferred(sc.name, "reading", _ctor(BME280, sc.name, bus, n, addr)))
            elif sc.type == "gpio":
                from .gpio import GPIOInput

                key, pin = str(o.get("key", "active")), _int(o, "pin")
                aw, pull = o.get("active_when", "high"), o.get("pull", "up")
                if aw not in ("high", "low") or pull not in ("up", "down"):
                    raise ConfigError("active_when must be high/low and pull up/down")

                def gpio(
                    nm: str = sc.name, k: str = key, p: int = pin, a: Any = aw, pl: Any = pull
                ) -> Sensor:
                    return GPIOInput(nm, k, p, active_when=a, pull=pl)

                out.append(Deferred(sc.name, "state", gpio))
            elif sc.type == "sim-battery":
                out.append(
                    sim.battery(
                        sc.name,
                        float_v=float(o.get("float_v", 13.4)),
                        drift_v_per_day=float(o.get("drift_v_per_day", 0.0)),
                    )
                )
            elif sc.type == "sim-environment":
                out.append(sim.environment(sc.name, base_c=float(o.get("base_c", 22.0))))
            elif sc.type == "sim-switch":
                out.append(
                    sim.switch(
                        sc.name, str(o.get("key", "active")), active=bool(o.get("active", False))
                    )
                )
            else:
                raise ConfigError(
                    "unknown type (ina226, ina219, bme280, gpio, sim-battery, "
                    "sim-environment, sim-switch)"
                )
        except ConfigError as e:
            raise ConfigError(f"{where}: {e}") from e
    return out
