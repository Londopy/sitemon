"""What every sensor looks like to the agent."""

from __future__ import annotations

from typing import Literal, Protocol

Value = float | bool | str


class SensorError(Exception):
    """A read failed. The agent counts these and reports a fault when they persist."""


class Sensor(Protocol):
    name: str
    kind: Literal["reading", "state"]

    def read(self) -> dict[str, Value]:
        """Blocking read. Raises SensorError on failure. Called from a worker thread."""
        ...

    def close(self) -> None: ...
