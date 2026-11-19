"""TI ADS1115 16-bit ADC, read one single-ended input at a time."""

from __future__ import annotations

import time
from collections.abc import Callable

from .base import SensorError
from .i2c import I2CBus

# Registers. Both are 16 bits, most significant byte first.
_CONVERSION = 0x00
_CONFIG = 0x01

_OS = 0x8000  # config bit 15: writing 1 starts a conversion, and it reads 0 until done


def config_word(channel: int) -> int:
    """The config register value that starts one conversion of AIN``channel`` against ground."""
    if channel not in range(4):
        raise ValueError(f"ADS1115 channel must be 0-3, not {channel!r}")
    mux = 0b100 + channel  # AINx against GND
    pga = 0b001  # +/-4.096 V, the smallest range that covers a 3.3 V input
    mode = 1  # single-shot
    dr = 0b100  # 128 samples a second
    comp_que = 0b11  # comparator off
    return _OS | mux << 12 | pga << 9 | mode << 8 | dr << 5 | comp_que


class ADS1115:
    """An ADS1115 at 0x48-0x4B, as set by its ADDR pin."""

    def __init__(
        self, bus: I2CBus, address: int = 0x48, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.bus = bus
        self.address = address
        self._sleep = sleep

    def read_volts(self, channel: int) -> float:
        """Volts on AIN``channel`` (0-3) against ground, from a new conversion."""
        self.bus.write_word_be(self.address, _CONFIG, config_word(channel))
        # A conversion takes 7.8 ms at 128 samples a second, 10% more on a slow clock.
        for _ in range(25):
            self._sleep(0.002)
            if self._register(_CONFIG) & _OS:
                return self._register(_CONVERSION, signed=True) * 4.096 / 32768
        raise SensorError(
            f"ADS1115 at 0x{self.address:02x}: AIN{channel} conversion not done after 50 ms"
        )

    def _register(self, reg: int, *, signed: bool = False) -> int:
        return int.from_bytes(self.bus.read_block(self.address, reg, 2), "big", signed=signed)
