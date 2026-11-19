"""Bosch BME280 temperature, humidity and pressure, with the datasheet's
compensation formulas as plain functions so they can be tested without a bus."""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .base import SensorError, Value
from .i2c import I2CBus

# Registers
_CHIP_ID = 0xD0
_CALIB_TP = 0x88  # 26 bytes: T1-T3 and P1-P9, then H1 at 0xA1
_CALIB_H = 0xE1  # 7 bytes: H2-H6
_CTRL_HUM = 0xF2
_CTRL_MEAS = 0xF4
_CONFIG = 0xF5
_DATA = 0xF7  # 8 bytes: pressure, temperature, humidity

_BME280 = 0x60
_BMP280 = 0x58

# x1 oversampling, filter off, one measurement a second: the chip barely warms itself.
_SLEEP = 0x00
_HUMIDITY_X1 = 0x01
_NORMAL_X1 = 0x27  # temperature x1, pressure x1, normal mode
_STANDBY_1S = 0xA0  # 1000 ms between measurements, filter off

_NOT_MEASURED = 0x80000


@dataclass(frozen=True)
class Calibration:
    """A chip's compensation constants, the datasheet's dig_T1 to dig_H6."""

    t1: int
    t2: int
    t3: int
    p1: int
    p2: int
    p3: int
    p4: int
    p5: int
    p6: int
    p7: int
    p8: int
    p9: int
    h1: int
    h2: int
    h3: int
    h4: int
    h5: int
    h6: int


def _signed8(v: int) -> int:
    return v - 0x100 if v & 0x80 else v


def _signed12(v: int) -> int:
    return v - 0x1000 if v & 0x800 else v


def parse_calibration(tp: Sequence[int], hum: Sequence[int]) -> Calibration:
    """Calibration from the 26 bytes at 0x88 and the 7 bytes at 0xE1 (little-endian)."""
    t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = struct.unpack_from("<HhhHhhhhhhhh", bytes(tp))
    h2, h3 = struct.unpack_from("<hB", bytes(hum))
    e4, e5, e6 = hum[3], hum[4], hum[5]
    return Calibration(
        t1=t1,
        t2=t2,
        t3=t3,
        p1=p1,
        p2=p2,
        p3=p3,
        p4=p4,
        p5=p5,
        p6=p6,
        p7=p7,
        p8=p8,
        p9=p9,
        h1=tp[25],
        h2=h2,
        h3=h3,
        # H4 and H5 are 12-bit and share the byte at 0xE5, a nibble each.
        h4=_signed12((e4 << 4) | (e5 & 0x0F)),
        h5=_signed12((e6 << 4) | (e5 >> 4)),
        h6=_signed8(hum[6]),
    )


def compensate_temperature(adc_t: int, cal: Calibration) -> tuple[float, float]:
    """Degrees Celsius, and t_fine, which the pressure and humidity formulas need."""
    var1 = (adc_t / 16384.0 - cal.t1 / 1024.0) * cal.t2
    var2 = ((adc_t / 131072.0 - cal.t1 / 8192.0) ** 2) * cal.t3
    t_fine = var1 + var2
    return t_fine / 5120.0, t_fine


def compensate_pressure(adc_p: int, t_fine: float, cal: Calibration) -> float:
    """Pascals."""
    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * cal.p6 / 32768.0
    var2 += var1 * cal.p5 * 2.0
    var2 = var2 / 4.0 + cal.p4 * 65536.0
    var1 = (cal.p3 * var1 * var1 / 524288.0 + cal.p2 * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * cal.p1
    if var1 == 0:
        # In practice only with P1 = 0: calibration that didn't read back.
        raise SensorError("BME280 pressure calibration is invalid (would divide by zero)")
    p = 1048576.0 - adc_p
    p = (p - var2 / 4096.0) * 6250.0 / var1
    var1 = cal.p9 * p * p / 2147483648.0
    var2 = p * cal.p8 / 32768.0
    return p + (var1 + var2 + cal.p7) / 16.0


def compensate_humidity(adc_h: int, t_fine: float, cal: Calibration) -> float:
    """Relative humidity in percent."""
    h = t_fine - 76800.0
    h = (adc_h - (cal.h4 * 64.0 + cal.h5 / 16384.0 * h)) * (
        cal.h2 / 65536.0 * (1.0 + cal.h6 / 67108864.0 * h * (1.0 + cal.h3 / 67108864.0 * h))
    )
    h = h * (1.0 - cal.h1 * h / 524288.0)
    # The formula runs past the physical range near condensation and in very
    # dry air; the datasheet clamps it the same way.
    return min(max(h, 0.0), 100.0)


class BME280:
    """A BME280 at 0x76 (SDO tied to ground) or 0x77 (SDO tied high).

    It runs in normal mode, measuring on its own once a second, and ``read``
    fetches the latest result. ``close`` leaves the bus open: it is shared
    with other sensors and closed by whoever opened it.
    """

    kind: Literal["reading", "state"] = "reading"

    def __init__(self, name: str, bus: I2CBus, address: int = 0x76) -> None:
        self.name = name
        self.bus = bus
        self.address = address
        self.resets = 0
        chip_id = bus.read_block(address, _CHIP_ID, 1)[0]
        if chip_id != _BME280:
            # BMP280 boards look the same and are often sold as BME280s.
            hint = "; 0x58 is a BMP280, which has no humidity sensor" if chip_id == _BMP280 else ""
            raise SensorError(
                f"{name}: no BME280 at 0x{address:02x} "
                f"(chip id 0x{chip_id:02x}, expected 0x{_BME280:02x}{hint})"
            )
        self._cal = self._read_calibration()
        self._configure()

    def read(self) -> dict[str, Value]:
        if self.bus.read_block(self.address, _CTRL_MEAS, 1)[0] != _NORMAL_X1:
            self.resets += 1
            self._configure()
            # A "reset" chip may be a replacement with its own calibration.
            self._cal = self._read_calibration()
        d = self.bus.read_block(self.address, _DATA, 8)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7]
        if adc_t == _NOT_MEASURED:
            raise SensorError(
                f"{self.name}: BME280 at 0x{self.address:02x} has no measurement yet "
                "(just started or reset)"
            )
        temp_c, t_fine = compensate_temperature(adc_t, self._cal)
        return {
            "temp_c": round(temp_c, 2),
            "rh": round(compensate_humidity(adc_h, t_fine, self._cal), 1),
            "hpa": round(compensate_pressure(adc_p, t_fine, self._cal) / 100.0, 1),
        }

    def close(self) -> None:
        pass

    def _read_calibration(self) -> Calibration:
        return parse_calibration(
            self.bus.read_block(self.address, _CALIB_TP, 26),
            self.bus.read_block(self.address, _CALIB_H, 7),
        )

    def _configure(self) -> None:
        a = self.address
        # Config writes may be ignored in normal mode, so sleep first; the last
        # ctrl_meas write also makes ctrl_hum take effect.
        self.bus.write_byte(a, _CTRL_MEAS, _SLEEP)
        self.bus.write_byte(a, _CTRL_HUM, _HUMIDITY_X1)
        self.bus.write_byte(a, _CONFIG, _STANDBY_1S)
        self.bus.write_byte(a, _CTRL_MEAS, _NORMAL_X1)
