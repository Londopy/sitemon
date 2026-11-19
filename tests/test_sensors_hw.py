"""Hardware sensor drivers, against a fake I2C bus and fake GPIO pins.

Nothing here needs an I2C bus, GPIO or the [pi] extra: FakeSMBus stands in for
smbus2 and the chips on the bus, and fake devices for gpiozero.
"""

from __future__ import annotations

import dataclasses
import struct
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Literal

import pytest

from sitemon.agent.sensors.base import SensorError
from sitemon.agent.sensors.bme280 import (
    BME280,
    Calibration,
    compensate_humidity,
    compensate_pressure,
    compensate_temperature,
    parse_calibration,
)
from sitemon.agent.sensors.gpio import GPIOInput
from sitemon.agent.sensors.i2c import I2CBus
from sitemon.agent.sensors.ina import INA219, INA226

Regs = dict[tuple[int, int], bytes]


class FakeSMBus:
    """Stands in for ``smbus2.SMBus`` and the chips on the bus.

    ``regs`` maps (address, register) to that register's bytes: two for the
    INA chips' 16-bit registers, one for the BME280's. A block read runs on
    into the following registers, as the BME280's auto-increment does. Writes
    are logged and land in ``regs``. ``fail_next`` makes the next N transfers
    raise OSError the way RF interference does; ``fail_opens`` does the same
    for opening the bus.
    """

    def __init__(self, regs: Regs | None = None) -> None:
        self.regs: Regs = dict(regs or {})
        self.writes: list[tuple[int, int, list[int]]] = []
        self.reads: list[tuple[int, int, int]] = []
        self.fail_next = 0
        self.fail_opens = 0
        self.opens = 0
        self.closes = 0
        self.is_open = False

    def open(self, bus: int) -> FakeSMBus:
        """The opener for I2CBus. Every handle it hands out is this object."""
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError(19, "No such device")
        self.opens += 1
        self.is_open = True
        return self

    def close(self) -> None:
        self.closes += 1
        self.is_open = False

    def _transfer(self) -> None:
        assert self.is_open, "transfer on a closed handle"
        if self.fail_next:
            self.fail_next -= 1
            raise OSError(5, "Input/output error")

    def read_i2c_block_data(self, addr: int, reg: int, n: int) -> list[int]:
        self._transfer()
        self.reads.append((addr, reg, n))
        out: list[int] = []
        while len(out) < n:
            out += self.regs[(addr, reg)]
            reg += 1
        return out[:n]

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self._transfer()
        self.writes.append((addr, reg, [value]))
        self.regs[(addr, reg)] = bytes([value])

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        self._transfer()
        self.writes.append((addr, reg, list(data)))
        self.regs[(addr, reg)] = bytes(data)


def make_bus(fake: FakeSMBus, retries: int = 3) -> I2CBus:
    # No backoff: these tests count retries, they don't need to wait them out.
    return I2CBus(1, opener=fake.open, retries=retries, backoff_s=0)


def be16(value: int) -> bytes:
    """A 16-bit register as the INA chips hold it, negative values in two's complement."""
    return (value & 0xFFFF).to_bytes(2, "big")


def test_i2c_reads_and_writes() -> None:
    fake = FakeSMBus({(0x40, 0x02): be16(0x2580)})
    bus = make_bus(fake)
    assert bus.read_block(0x40, 0x02, 2) == [0x25, 0x80]
    bus.write_byte(0x76, 0xF2, 0x01)
    bus.write_word_be(0x40, 0x00, 0x4527)
    assert fake.writes == [(0x76, 0xF2, [0x01]), (0x40, 0x00, [0x45, 0x27])]
    assert bus.recoveries == 0


def test_i2c_transient_error_is_retried_on_a_reopened_handle() -> None:
    fake = FakeSMBus({(0x40, 0x02): be16(0x2580)})
    bus = make_bus(fake)
    fake.fail_next = 1
    assert bus.read_block(0x40, 0x02, 2) == [0x25, 0x80]
    assert bus.recoveries == 1
    assert (fake.opens, fake.closes) == (2, 1)


def test_i2c_backoff_doubles_between_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("sitemon.agent.sensors.i2c.time.sleep", sleeps.append)
    fake = FakeSMBus({(0x40, 0x02): be16(0)})
    bus = I2CBus(1, opener=fake.open)  # the defaults: 3 retries, 20 ms first backoff
    fake.fail_next = 3
    assert bus.read_block(0x40, 0x02, 2) == [0, 0]
    assert sleeps == pytest.approx([0.02, 0.04, 0.08])
    assert bus.recoveries == 3


def test_i2c_persistent_error_becomes_a_sensor_error() -> None:
    fake = FakeSMBus({(0x40, 0x02): be16(0)})
    bus = make_bus(fake, retries=3)
    fake.fail_next = 100
    with pytest.raises(SensorError, match="device 0x40: read of register 0x02 failed"):
        bus.read_block(0x40, 0x02, 2)
    assert fake.fail_next == 96  # one try and three retries
    # Once the interference stops, the same bus works again.
    fake.fail_next = 0
    assert bus.read_block(0x40, 0x02, 2) == [0, 0]


def test_i2c_failed_reopen_is_retried_too() -> None:
    fake = FakeSMBus({(0x40, 0x02): be16(0x2580)})
    bus = make_bus(fake)
    fake.fail_next = 1
    fake.fail_opens = 1
    assert bus.read_block(0x40, 0x02, 2) == [0x25, 0x80]
    assert bus.recoveries == 1


def test_i2c_open_failure_is_a_sensor_error() -> None:
    fake = FakeSMBus()
    fake.fail_opens = 1
    with pytest.raises(SensorError, match="cannot open I2C bus 1"):
        make_bus(fake)


def test_i2c_closed_bus_refuses_transfers() -> None:
    fake = FakeSMBus({(0x40, 0x02): be16(0)})
    bus = make_bus(fake)
    bus.close()
    assert not fake.is_open
    with pytest.raises(SensorError, match="closed"):
        bus.read_block(0x40, 0x02, 2)
    assert fake.opens == 1  # it did not quietly reopen


@pytest.mark.parametrize("kwargs", [{"retries": -1}, {"backoff_s": -0.1}])
def test_i2c_rejects_negative_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        I2CBus(1, opener=FakeSMBus().open, **kwargs)


def test_i2c_default_opener_uses_smbus2(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSMBus()
    opened: list[int] = []

    def smbus(n: int) -> FakeSMBus:
        opened.append(n)
        return fake.open(n)

    smbus2 = types.ModuleType("smbus2")
    setattr(smbus2, "SMBus", smbus)
    monkeypatch.setitem(sys.modules, "smbus2", smbus2)
    I2CBus(3)
    assert opened == [3]


def ina226_regs(shunt: int, bus_raw: int) -> Regs:
    return {
        (0x40, 0x00): be16(0x4127),  # power-on default
        (0x40, 0x01): be16(shunt),
        (0x40, 0x02): be16(bus_raw),
        (0x40, 0xFE): be16(0x5449),  # "TI"
    }


def test_ina226_configures_and_converts() -> None:
    # 0x2580 is 9600 counts of 1.25 mV: 12.0 V. 5 A through 2 mOhm is 10 mV,
    # 4000 counts of 2.5 uV.
    fake = FakeSMBus(ina226_regs(shunt=4000, bus_raw=0x2580))
    ina = INA226("batt", make_bus(fake), 0x40, 0.002)
    assert fake.writes == [(0x40, 0x00, [0x45, 0x27])]
    assert ina.kind == "reading"
    assert ina.read() == {"v": 12.0, "a": 5.0, "w": 60.0}
    assert ina.resets == 0
    assert len(fake.writes) == 1
    # Current comes from the shunt voltage: the current and calibration
    # registers (0x04, 0x05) are never touched.
    assert {reg for _, reg, _ in fake.reads} == {0x00, 0x01, 0x02, 0xFE}


def test_ina226_negative_current() -> None:
    # Charging: current runs the other way through the shunt.
    fake = FakeSMBus(ina226_regs(shunt=-4000, bus_raw=0x2580))
    ina = INA226("batt", make_bus(fake), 0x40, 0.002)
    assert ina.read() == {"v": 12.0, "a": -5.0, "w": -60.0}


def test_ina226_rejects_the_wrong_manufacturer_id() -> None:
    regs = ina226_regs(shunt=0, bus_raw=0)
    regs[(0x40, 0xFE)] = be16(0x0000)
    fake = FakeSMBus(regs)
    with pytest.raises(SensorError, match="no INA226 at 0x40"):
        INA226("batt", make_bus(fake), 0x40, 0.002)
    assert fake.writes == []  # never configures a chip it doesn't recognize


def test_ina226_rewrites_its_configuration_after_a_reset() -> None:
    fake = FakeSMBus(ina226_regs(shunt=4000, bus_raw=0x2580))
    ina = INA226("batt", make_bus(fake), 0x40, 0.002)
    fake.regs[(0x40, 0x00)] = be16(0x4127)  # reset: back to the power-on default
    # The read that finds the reset is skipped: its data registers may be 0.
    with pytest.raises(SensorError, match="reset"):
        ina.read()
    assert ina.resets == 1
    assert fake.regs[(0x40, 0x00)] == be16(0x4527)
    assert ina.read() == {"v": 12.0, "a": 5.0, "w": 60.0}
    assert ina.resets == 1
    assert len(fake.writes) == 2  # setup, then the one rewrite


def test_ina_rejects_a_shunt_that_is_not_positive() -> None:
    fake = FakeSMBus(ina226_regs(shunt=0, bus_raw=0))
    with pytest.raises(ValueError):
        INA226("batt", make_bus(fake), 0x40, 0.0)


def ina219_regs(shunt: int, bus_raw: int) -> Regs:
    return {
        (0x41, 0x00): be16(0x399F),
        (0x41, 0x01): be16(shunt),
        (0x41, 0x02): be16(bus_raw),
    }


def test_ina219_configures_and_converts() -> None:
    # 1.5 A through 0.1 ohm is 150 mV, 15000 counts of 10 uV. 12.0 V is 3000
    # steps of 4 mV in bits 15-3; bit 1 (conversion ready) is set and ignored.
    fake = FakeSMBus(ina219_regs(shunt=15000, bus_raw=(3000 << 3) | 0b10))
    ina = INA219("solar", make_bus(fake), 0x41, 0.1)
    assert fake.writes == [(0x41, 0x00, [0x39, 0x9F])]
    assert ina.kind == "reading"
    assert ina.read() == {"v": 12.0, "a": 1.5, "w": 18.0}
    assert ina.resets == 0


def test_ina219_negative_current() -> None:
    fake = FakeSMBus(ina219_regs(shunt=-15000, bus_raw=3000 << 3))
    ina = INA219("solar", make_bus(fake), 0x41, 0.1)
    assert ina.read() == {"v": 12.0, "a": -1.5, "w": -18.0}


def test_ina219_math_overflow_is_a_sensor_error() -> None:
    fake = FakeSMBus(ina219_regs(shunt=15000, bus_raw=(3000 << 3) | 0b1))
    ina = INA219("solar", make_bus(fake), 0x41, 0.1)
    with pytest.raises(SensorError, match="overflow"):
        ina.read()


def test_ina219_rewrites_a_configuration_that_reads_back_wrong() -> None:
    fake = FakeSMBus(ina219_regs(shunt=15000, bus_raw=3000 << 3))
    ina = INA219("solar", make_bus(fake), 0x41, 0.1)
    fake.regs[(0x41, 0x00)] = be16(0x0000)
    with pytest.raises(SensorError, match="reset"):
        ina.read()
    assert ina.resets == 1
    assert fake.regs[(0x41, 0x00)] == be16(0x399F)
    assert ina.read() == {"v": 12.0, "a": 1.5, "w": 18.0}


#: The calibration from the datasheet's worked example. The example stops at
#: pressure, so the humidity constants are ones typical of real chips.
BOSCH = Calibration(
    t1=27504,
    t2=26435,
    t3=-1000,
    p1=36477,
    p2=-10685,
    p3=3024,
    p4=2855,
    p5=140,
    p6=-7,
    p7=15500,
    p8=-14600,
    p9=6000,
    h1=75,
    h2=362,
    h3=0,
    h4=313,
    h5=50,
    h6=30,
)
ADC_T, ADC_P, ADC_H = 519888, 415148, 28200


def bosch_t_fine_int32(adc_t: int, c: Calibration) -> int:
    """t_fine by the datasheet's 32-bit integer formula."""
    var1 = (((adc_t >> 3) - (c.t1 << 1)) * c.t2) >> 11
    var2 = (((((adc_t >> 4) - c.t1) * ((adc_t >> 4) - c.t1)) >> 12) * c.t3) >> 14
    return var1 + var2


def bosch_humidity_int32(adc_h: int, t_fine: int, c: Calibration) -> float:
    """%RH by the datasheet's 32-bit integer formula, an independent check on the float one."""
    v = t_fine - 76800
    a = (((adc_h << 14) - (c.h4 << 20) - (c.h5 * v)) + 16384) >> 15
    b = (
        (((((v * c.h6) >> 10) * (((v * c.h3) >> 11) + 32768)) >> 10) + 2097152) * c.h2 + 8192
    ) >> 14
    v = a * b
    v -= ((((v >> 15) * (v >> 15)) >> 7) * c.h1) >> 4
    v = min(max(v, 0), 419430400)
    return (v >> 12) / 1024


def test_bme280_temperature_and_pressure_match_the_datasheet_example() -> None:
    temp_c, t_fine = compensate_temperature(ADC_T, BOSCH)
    assert temp_c == pytest.approx(25.08, abs=0.005)
    assert t_fine == pytest.approx(128422, abs=1)
    assert compensate_pressure(ADC_P, t_fine, BOSCH) == pytest.approx(100653.27, abs=0.01)


def test_bme280_humidity_agrees_with_the_integer_formula() -> None:
    t_fine_int = bosch_t_fine_int32(ADC_T, BOSCH)
    assert t_fine_int == 128422  # the datasheet's value: the integer version is transcribed right
    _, t_fine = compensate_temperature(ADC_T, BOSCH)
    for adc_h in (25000, ADC_H, 30000, 35000):
        expected = bosch_humidity_int32(adc_h, t_fine_int, BOSCH)
        assert compensate_humidity(adc_h, t_fine, BOSCH) == pytest.approx(expected, abs=0.01)
    assert 40 < compensate_humidity(ADC_H, t_fine, BOSCH) < 50  # plausible indoor air


def test_bme280_humidity_is_clamped_to_0_100() -> None:
    _, t_fine = compensate_temperature(ADC_T, BOSCH)
    assert compensate_humidity(0, t_fine, BOSCH) == 0.0
    assert compensate_humidity(0xFFFF, t_fine, BOSCH) == 100.0


def test_bme280_pressure_with_zeroed_calibration_is_a_sensor_error() -> None:
    _, t_fine = compensate_temperature(ADC_T, BOSCH)
    with pytest.raises(SensorError):
        compensate_pressure(ADC_P, t_fine, dataclasses.replace(BOSCH, p1=0))


def calibration_bytes(c: Calibration) -> tuple[bytes, bytes]:
    """``c`` laid out as the chip stores it: 26 bytes at 0x88 and 7 at 0xE1."""
    tp = struct.pack(
        "<HhhHhhhhhhhh",
        c.t1,
        c.t2,
        c.t3,
        c.p1,
        c.p2,
        c.p3,
        c.p4,
        c.p5,
        c.p6,
        c.p7,
        c.p8,
        c.p9,
    )
    tp += bytes([0x00, c.h1])  # 0xA0 is unused; H1 is at 0xA1
    h4, h5 = c.h4 & 0xFFF, c.h5 & 0xFFF
    hum = struct.pack("<hB", c.h2, c.h3)
    hum += bytes([h4 >> 4, (h4 & 0x0F) | (h5 & 0x0F) << 4, h5 >> 4])
    hum += struct.pack("<b", c.h6)
    return tp, hum


def test_bme280_calibration_parsing() -> None:
    tp, _ = calibration_bytes(BOSCH)
    # H2 = -2, H3 = 0, then H4 and H5 sharing the byte at 0xE5, then H6 = -3.
    c = parse_calibration(tp, bytes([0xFE, 0xFF, 0x00, 0x13, 0x92, 0x03, 0xFD]))
    assert (c.t1, c.t2, c.t3, c.p1, c.p9) == (27504, 26435, -1000, 36477, 6000)
    assert (c.h1, c.h2, c.h3, c.h4, c.h5, c.h6) == (75, -2, 0, 0x132, 0x039, -3)
    # H4 = 0xF05 and H5 = 0xFFF are negative as 12-bit numbers.
    c = parse_calibration(tp, bytes([0x6A, 0x01, 0x00, 0xF0, 0xF5, 0xFF, 0x1E]))
    assert (c.h4, c.h5) == (-251, -1)
    # The encoder the driver tests use round-trips.
    odd = dataclasses.replace(BOSCH, h4=-251, h5=-1, h6=-3)
    assert parse_calibration(*calibration_bytes(BOSCH)) == BOSCH
    assert parse_calibration(*calibration_bytes(odd)) == odd


def bme280_data(adc_p: int, adc_t: int, adc_h: int) -> bytes:
    """The 8 bytes at 0xF7: 20-bit pressure and temperature, most significant
    bits first and left-aligned, then 16-bit humidity."""
    return bytes(
        [
            adc_p >> 12,
            (adc_p >> 4) & 0xFF,
            (adc_p & 0x0F) << 4,
            adc_t >> 12,
            (adc_t >> 4) & 0xFF,
            (adc_t & 0x0F) << 4,
            adc_h >> 8,
            adc_h & 0xFF,
        ]
    )


#: The data registers' reset values: nothing measured yet.
NOT_MEASURED = bme280_data(0x80000, 0x80000, 0x8000)
#: The writes that set the chip up, as (register, data).
SETUP = [(0xF4, [0x00]), (0xF2, [0x01]), (0xF5, [0xA0]), (0xF4, [0x27])]


def put(regs: Regs, start: int, block: bytes) -> None:
    """Store a run of one-byte BME280 registers at 0x76."""
    for i, b in enumerate(block):
        regs[(0x76, start + i)] = bytes([b])


def bme280_regs(cal: Calibration, data: bytes, chip_id: int = 0x60) -> Regs:
    """A BME280 at 0x76 as it powers up: asleep, with default settings."""
    tp, hum = calibration_bytes(cal)
    regs: Regs = {}
    put(regs, 0xD0, bytes([chip_id]))
    put(regs, 0x88, tp)
    put(regs, 0xE1, hum)
    put(regs, 0xF2, bytes(4))  # ctrl_hum, status, ctrl_meas, config
    put(regs, 0xF7, data)
    return regs


def test_bme280_sets_up_the_chip_and_reads_through_the_bus() -> None:
    fake = FakeSMBus(bme280_regs(BOSCH, bme280_data(ADC_P, ADC_T, ADC_H)))
    env = BME280("env", make_bus(fake))
    assert [(reg, data) for _, reg, data in fake.writes] == SETUP
    assert env.kind == "reading"
    # 25.08 C and 100653.27 Pa, as in the datasheet example.
    assert env.read() == {"temp_c": 25.08, "rh": 45.0, "hpa": 1006.5}
    assert env.resets == 0


def test_bme280_rejects_a_bmp280() -> None:
    fake = FakeSMBus(bme280_regs(BOSCH, NOT_MEASURED, chip_id=0x58))
    with pytest.raises(SensorError, match="chip id 0x58.*BMP280"):
        BME280("env", make_bus(fake))
    assert fake.writes == []


def test_bme280_without_a_measurement_yet_is_a_sensor_error() -> None:
    fake = FakeSMBus(bme280_regs(BOSCH, NOT_MEASURED))
    env = BME280("env", make_bus(fake))
    with pytest.raises(SensorError, match="no measurement yet"):
        env.read()
    assert env.resets == 0


def test_bme280_reset_is_detected_and_the_chip_set_up_again() -> None:
    fake = FakeSMBus(bme280_regs(BOSCH, bme280_data(ADC_P, ADC_T, ADC_H)))
    env = BME280("env", make_bus(fake))
    env.read()
    # A reset: asleep with default settings, data registers at their reset values.
    put(fake.regs, 0xF2, bytes(4))
    put(fake.regs, 0xF7, NOT_MEASURED)
    fake.writes.clear()
    fake.reads.clear()
    with pytest.raises(SensorError, match="no measurement yet"):
        env.read()
    assert env.resets == 1
    assert [(reg, data) for _, reg, data in fake.writes] == SETUP
    assert {(0x88, 26), (0xE1, 7)} <= {(reg, n) for _, reg, n in fake.reads}  # calibration re-read
    # The first measurement since the reset lands, and reads are back to normal.
    put(fake.regs, 0xF7, bme280_data(ADC_P, ADC_T, ADC_H))
    assert env.read() == {"temp_c": 25.08, "rh": 45.0, "hpa": 1006.5}
    assert env.resets == 1


class FakePin:
    """What GPIOInput needs from a device: the pin level and close()."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("active_when", "level", "expected"),
    [("high", 1, True), ("high", 0, False), ("low", 0, True), ("low", 1, False)],
)
def test_gpio_input_is_true_when_the_level_matches_active_when(
    active_when: Literal["high", "low"], level: int, expected: bool
) -> None:
    pin = FakePin(level)
    sensor = GPIOInput("in", "on", 17, active_when=active_when, device_factory=lambda n, up: pin)
    assert sensor.kind == "state"
    assert sensor.read() == {"on": expected}


def test_gpio_input_opens_the_pin_with_the_requested_pull() -> None:
    opened: list[tuple[int, bool]] = []

    def factory(pin: int, pull_up: bool) -> FakePin:
        opened.append((pin, pull_up))
        return FakePin(0)

    GPIOInput("door", "open", 17, device_factory=factory)
    GPIOInput("water", "wet", 27, pull="down", device_factory=factory)
    assert opened == [(17, True), (27, False)]


def test_gpio_device_errors_become_sensor_errors() -> None:
    class Vanished:
        @property
        def value(self) -> int:
            raise RuntimeError("pin gone")

        def close(self) -> None:
            raise RuntimeError("pin gone")

    sensor = GPIOInput("door", "open", 17, device_factory=lambda n, up: Vanished())
    with pytest.raises(SensorError, match="pin gone"):
        sensor.read()
    with pytest.raises(SensorError, match="pin gone"):
        sensor.close()

    def busy(pin: int, pull_up: bool) -> FakePin:
        raise RuntimeError("GPIO17 is in use")

    with pytest.raises(SensorError, match="in use"):
        GPIOInput("door", "open", 17, device_factory=busy)


def test_gpio_close_closes_the_device() -> None:
    pin = FakePin(1)
    GPIOInput("door", "open", 17, device_factory=lambda n, up: pin).close()
    assert pin.closed


@pytest.mark.parametrize("kwargs", [{"active_when": "hi"}, {"pull": "none"}])
def test_gpio_rejects_bad_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GPIOInput("door", "open", 17, device_factory=lambda n, up: FakePin(0), **kwargs)


class FakeDigitalInputDevice:
    """Behaves like gpiozero's: ``value`` is 1 while the input is active, and
    with the pull-up on, active means the pin is low. ``pin.state`` is the level."""

    def __init__(self, pin: int, *, pull_up: bool = False) -> None:
        self.pin = types.SimpleNamespace(number=pin, state=1 if pull_up else 0)
        self.pull_up = pull_up
        self.closed = False

    @property
    def value(self) -> int:
        active_state = 0 if self.pull_up else 1
        return int(self.pin.state == active_state)

    def close(self) -> None:
        self.closed = True


def test_gpio_default_device_goes_by_the_pin_level(monkeypatch: pytest.MonkeyPatch) -> None:
    # Taken at face value, gpiozero's value would report a shut door as open.
    devices: list[FakeDigitalInputDevice] = []

    def device(pin: int, *, pull_up: bool = False) -> FakeDigitalInputDevice:
        devices.append(FakeDigitalInputDevice(pin, pull_up=pull_up))
        return devices[-1]

    gpiozero = types.ModuleType("gpiozero")
    setattr(gpiozero, "DigitalInputDevice", device)
    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)

    door = GPIOInput("door", "open", 17, active_when="high", pull="up")
    (dev,) = devices
    assert (dev.pin.number, dev.pull_up) == (17, True)
    dev.pin.state = 0  # shut: the magnet closes the reed switch to ground
    assert door.read() == {"open": False}
    dev.pin.state = 1  # open: the pull-up takes the pin high
    assert door.read() == {"open": True}
    door.close()
    assert dev.closed


def test_hardware_libraries_are_not_imported_with_the_drivers() -> None:
    # smbus2 and gpiozero are the optional [pi] extra; the hub and simulated
    # sites import these modules without them.
    code = (
        "import sys\n"
        "import sitemon.agent.sensors.bme280, sitemon.agent.sensors.gpio\n"
        "import sitemon.agent.sensors.i2c, sitemon.agent.sensors.ina\n"
        "print(sorted({'smbus2', 'gpiozero'} & set(sys.modules)))\n"
    )
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
