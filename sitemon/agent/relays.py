"""Relay outputs on the Pi's GPIO pins, and the keep-alive pin for the
hardware cutoff.

Every relay starts off and has a time limit: a pulse relay (power-cycling a
radio, a doorbell) switches off after its pulse, a switch relay (shack lights)
after ``max_on``. If the agent stops toggling the keep-alive pin, the cutoff
circuit described in deploy/README.md removes power from all of them.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from ..config import RelayConfig


class RelayError(Exception):
    pass


class Output(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...


def gpio_output(pin: int, active_low: bool) -> Output:
    import gpiozero  # the optional [pi] extra

    out: Output = gpiozero.DigitalOutputDevice(pin, active_high=not active_low, initial_value=False)
    return out


class Relay:
    def __init__(self, cfg: RelayConfig, out: Output) -> None:
        self.cfg = cfg
        self.out = out
        self.until: float | None = None

    @property
    def is_on(self) -> bool:
        return self.until is not None


class RelayBank:
    def __init__(
        self,
        configs: list[RelayConfig],
        output: Callable[[int, bool], Output] = gpio_output,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.relays = {c.name: Relay(c, output(c.pin, c.active_low)) for c in configs}
        self.off_all()

    def states(self) -> dict[str, bool]:
        return {name: r.is_on for name, r in self.relays.items()}

    def run(self, name: str, action: str, seconds: float | None = None) -> str:
        """Carry out ``action`` on relay ``name``; returns what was done."""
        r = self.relays.get(name)
        if r is None:
            raise RelayError(f"no relay named {name!r}")
        if seconds is not None and not (math.isfinite(seconds) and seconds > 0):
            raise RelayError("seconds must be a positive number")
        if action == "off":
            _switch_off([r])
            return f"{name} off"
        if r.cfg.mode == "pulse":
            if action != "pulse":
                raise RelayError(f"{name} is a pulse relay: use pulse")
            limit = r.cfg.pulse
        else:
            if action != "on":
                raise RelayError(f"{name} is a switch relay: use on or off")
            limit = r.cfg.max_on
        hold = min(seconds or limit, limit)
        r.out.on()
        r.until = self.clock() + hold
        return f"{name} on for {hold:g} s"

    def tick(self) -> bool:
        """Switch off relays whose time is up. True if any changed."""
        now = self.clock()
        due = [r for r in self.relays.values() if r.until is not None and now >= r.until]
        _switch_off(due)
        return bool(due)

    def off_all(self) -> None:
        _switch_off(self.relays.values())

    def close(self) -> None:
        try:
            self.off_all()
        finally:
            for r in self.relays.values():
                r.out.close()


def _switch_off(relays: Iterable[Relay]) -> None:
    """Switch every one of ``relays`` off, even if one of them fails."""
    first: BaseException | None = None
    for r in relays:
        r.until = None
        try:
            r.out.off()
        # Whatever one output raises, the others still have to go off; the
        # first error is raised once they have.
        except Exception as e:
            first = first or e
    if first is not None:
        raise first


class KeepAlive:
    """Toggles a pin while the agent is healthy; the cutoff circuit drops the
    relays' power within a few seconds of the toggling stopping."""

    def __init__(self, pin: int, output: Callable[[int, bool], Output] = gpio_output) -> None:
        self.out = output(pin, False)
        self.level = False

    def toggle(self) -> None:
        self.level = not self.level
        if self.level:
            self.out.on()
        else:
            self.out.off()

    def close(self) -> None:
        try:
            self.out.off()
        finally:
            self.out.close()
