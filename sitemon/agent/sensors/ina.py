"""TI INA226 and INA219 power monitors.

Current is computed from the shunt voltage, not the current register: that
register depends on a calibration value an RF-induced reset silently zeroes.
"""

from __future__ import annotations

from typing import Literal

from .base import SensorError, Value
from .i2c import I2CBus

# Registers. All are 16 bits, most significant byte first.
_CONFIG = 0x00
_SHUNT_VOLTAGE = 0x01
_BUS_VOLTAGE = 0x02
_MANUFACTURER_ID = 0xFE  # INA226 only

_TEXAS_INSTRUMENTS = 0x5449  # "TI" in ASCII


def _signed16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


class _INA:
    """What the two drivers share; subclasses supply the chip specifics."""

    kind: Literal["reading", "state"] = "reading"
    _settings: int
    _shunt_lsb_v: float

    def __init__(self, name: str, bus: I2CBus, address: int, shunt_ohms: float) -> None:
        if shunt_ohms <= 0:
            raise ValueError(f"{name}: shunt_ohms must be positive, not {shunt_ohms!r}")
        self.name = name
        self.bus = bus
        self.address = address
        self.shunt_ohms = shunt_ohms
        self.resets = 0
        self._identify()
        self._configure()

    def read(self) -> dict[str, Value]:
        if self._register(_CONFIG) != self._settings:
            self.resets += 1
            self._configure()
            # Right after a reset the data registers can still read 0, and one false
            # 0 V reading would raise a critical battery alert.
            raise SensorError(f"{self.name}: chip was reset; reconfigured, reading next time")
        shunt_v = _signed16(self._register(_SHUNT_VOLTAGE)) * self._shunt_lsb_v
        volts = self._bus_volts(self._register(_BUS_VOLTAGE))
        amps = shunt_v / self.shunt_ohms
        return {"v": round(volts, 3), "a": round(amps, 3), "w": round(volts * amps, 2)}

    def close(self) -> None:
        # The bus is shared; whoever opened it closes it.
        pass

    def _register(self, reg: int) -> int:
        hi, lo = self.bus.read_block(self.address, reg, 2)
        return (hi << 8) | lo

    def _configure(self) -> None:
        self.bus.write_word_be(self.address, _CONFIG, self._settings)

    def _identify(self) -> None:
        """Raise SensorError if the device at the address is the wrong chip."""

    def _bus_volts(self, raw: int) -> float:
        raise NotImplementedError


class INA226(_INA):
    """TI INA226: bus up to 36 V, shunt range +/-81.92 mV.

    Each reading averages 16 conversions of 1.1 ms, about 35 ms in all, which
    evens out switching-charger ripple that a single conversion would sample
    at a random point.
    """

    _settings = 0x4527  # 16-sample average, 1.1 ms conversions, shunt and bus continuous
    _shunt_lsb_v = 2.5e-6

    def _identify(self) -> None:
        # Other chips answer at 0x40-0x4F too; a config mistake should fail here.
        found = self._register(_MANUFACTURER_ID)
        if found != _TEXAS_INSTRUMENTS:
            raise SensorError(
                f"{self.name}: no INA226 at 0x{self.address:02x} "
                f"(manufacturer id 0x{found:04x}, expected 0x{_TEXAS_INSTRUMENTS:04x})"
            )

    def _bus_volts(self, raw: int) -> float:
        return raw * 1.25e-3


class INA219(_INA):
    """TI INA219: bus up to 26 V (32 V range), shunt range +/-320 mV.

    It has no id register to check. Its configuration here, 0x399F, is also
    its power-on default, so a reset leaves it measuring the same way and
    doesn't show up in ``resets``; the readback still catches a configuration
    register changed any other way.
    """

    _settings = 0x399F  # 32 V range, +/-320 mV, 12-bit, shunt and bus continuous
    _shunt_lsb_v = 10e-6

    def _bus_volts(self, raw: int) -> float:
        # Bits 15-3 hold the voltage in 4 mV steps; bit 1 is "conversion
        # ready" and bit 0 the math overflow flag.
        if raw & 0x0001:
            raise SensorError(
                f"{self.name}: INA219 at 0x{self.address:02x} flagged a math overflow (OVF)"
            )
        return (raw >> 3) * 4e-3
