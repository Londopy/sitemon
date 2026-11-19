"""Discrete inputs on the Pi's GPIO pins: door switches, water sensors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from .base import SensorError, Value


class _GpiozeroPin:
    """A gpiozero input whose ``value`` is the pin level, 1 high or 0 low.

    gpiozero's own ``value`` inverts with the pull-up on, which would flip
    every switch wired to ground, so this reads the raw pin state.
    """

    def __init__(self, pin: int, pull_up: bool) -> None:
        import gpiozero  # the optional [pi] extra

        self._device = gpiozero.DigitalInputDevice(pin, pull_up=pull_up)

    @property
    def value(self) -> int:
        return int(self._device.pin.state)

    def close(self) -> None:
        self._device.close()


class GPIOInput:
    """A discrete input read as ``{key: bool}``, True when the pin level is
    ``active_when``. A reed switch to ground with the pull-up on reads high
    when the door opens: ``GPIOInput("door", "open", 17, active_when="high")``.
    """

    kind: Literal["reading", "state"] = "state"

    def __init__(
        self,
        name: str,
        key: str,
        pin: int,
        *,
        active_when: Literal["high", "low"] = "high",
        pull: Literal["up", "down"] = "up",
        device_factory: Callable[[int, bool], Any] | None = None,
    ) -> None:
        # From a config file, where a typo would quietly invert the input.
        if active_when not in ("high", "low"):
            raise ValueError(f"{name}: active_when must be 'high' or 'low', not {active_when!r}")
        if pull not in ("up", "down"):
            raise ValueError(f"{name}: pull must be 'up' or 'down', not {pull!r}")
        self.name = name
        self.key = key
        self.pin = pin
        self.active_when = active_when
        self.pull = pull
        factory = device_factory if device_factory is not None else _GpiozeroPin
        # The pin backends raise plain Exception subclasses (lgpio.error), so
        # this can't be narrower; it becomes a sensor fault the hub sees.
        try:
            self._device: Any = factory(pin, pull == "up")
        except ImportError:
            raise
        except Exception as e:
            raise SensorError(f"{name}: cannot open GPIO{pin}: {e}") from e

    def read(self) -> dict[str, Value]:
        try:
            high = bool(self._device.value)
        except Exception as e:
            raise SensorError(f"{self.name}: reading GPIO{self.pin} failed: {e}") from e
        return {self.key: high == (self.active_when == "high")}

    def close(self) -> None:
        try:
            self._device.close()
        except Exception as e:
            raise SensorError(f"{self.name}: closing GPIO{self.pin} failed: {e}") from e
