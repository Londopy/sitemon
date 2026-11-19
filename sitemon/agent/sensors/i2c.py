"""An I2C bus that retries failed transfers on a fresh handle, since a nearby
transmitter makes the odd transfer fail."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from .base import SensorError

_T = TypeVar("_T")


def _open_smbus(bus: int) -> Any:
    import smbus2

    return smbus2.SMBus(bus)


class I2CBus:
    """One I2C bus (``/dev/i2c-<bus>``), shared by every sensor wired to it.

    ``opener(bus)`` returns an smbus2-style handle; tests pass one that returns
    a fake. The default opens the bus with smbus2. Addresses and registers in
    error messages are lowercase hex, as ``i2cdetect`` prints them.
    """

    def __init__(
        self,
        bus: int,
        *,
        opener: Callable[[int], Any] | None = None,
        retries: int = 3,
        backoff_s: float = 0.02,
    ) -> None:
        if retries < 0:
            raise ValueError(f"retries must be 0 or more, not {retries!r}")
        if backoff_s < 0:
            raise ValueError(f"backoff_s must be 0 or more, not {backoff_s!r}")
        self.number = bus
        self.retries = retries
        self.backoff_s = backoff_s
        self.recoveries = 0
        self._opener = opener if opener is not None else _open_smbus
        # smbus2 selects the device with a separate ioctl, so transfers from two
        # threads could each talk to the other's device.
        self._lock = threading.Lock()
        self._closed = False
        try:
            self._handle: Any = self._opener(bus)
        except OSError as e:
            raise SensorError(f"cannot open I2C bus {bus}: {e}") from e

    def read_block(self, addr: int, reg: int, n: int) -> list[int]:
        """Read ``n`` bytes starting at register ``reg`` of the device at ``addr``."""
        return self._transfer(
            addr,
            f"read of register 0x{reg:02x}",
            lambda h: list(h.read_i2c_block_data(addr, reg, n)),
        )

    def write_byte(self, addr: int, reg: int, value: int) -> None:
        self._transfer(
            addr,
            f"write of 0x{value:02x} to register 0x{reg:02x}",
            lambda h: h.write_byte_data(addr, reg, value),
        )

    def write_word_be(self, addr: int, reg: int, value: int) -> None:
        """Write a 16-bit register high byte first.

        SMBus word writes send the low byte first, which is backwards for chips
        with big-endian registers such as the INA226, so this goes out as a
        two-byte block instead.
        """
        data = [(value >> 8) & 0xFF, value & 0xFF]
        self._transfer(
            addr,
            f"write of 0x{value:04x} to register 0x{reg:02x}",
            lambda h: h.write_i2c_block_data(addr, reg, data),
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop_handle()

    def _transfer(self, addr: int, what: str, op: Callable[[Any], _T]) -> _T:
        with self._lock:
            if self._closed:
                raise SensorError(f"I2C bus {self.number} is closed")
            error: OSError | None = None
            for attempt in range(self.retries + 1):
                try:
                    if self._handle is None:
                        self._handle = self._opener(self.number)
                        self.recoveries += 1
                    return op(self._handle)
                except OSError as e:
                    error = e
                    self._drop_handle()
                    if attempt < self.retries:
                        time.sleep(self.backoff_s * 2**attempt)
            raise SensorError(
                f"I2C bus {self.number}, device 0x{addr:02x}: {what} failed "
                f"(retried {self.retries} times): {error}"
            ) from error

    def _drop_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass  # a handle that just failed may not close cleanly; it's discarded anyway
