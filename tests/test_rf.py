"""The ADS1115 ADC and the RF power sensor on it, against a fake I2C bus, and
the simulated RF sensor."""

from __future__ import annotations

from typing import Any

import pytest

from sitemon.agent.sensors import sim
from sitemon.agent.sensors.ads1115 import ADS1115, config_word
from sitemon.agent.sensors.base import SensorError, Value
from sitemon.agent.sensors.rf import RFPower

from .test_sensors_hw import FakeSMBus, be16, make_bus


class FakeADS1115(FakeSMBus):
    """An ADS1115 at 0x48 whose inputs AIN0-AIN3 convert to ``counts``.

    Its clock moves only when the driver calls ``sleep``. Writing the config
    register with OS set starts a conversion of the input MUX selects, which
    takes ``conversion_s``: until then OS reads 0 and the conversion register
    holds the previous result.
    """

    def __init__(self, counts: list[int], conversion_s: float = 1 / 128) -> None:
        # As powered up: idle, default configuration, no result yet.
        super().__init__({(0x48, 0x00): be16(0), (0x48, 0x01): be16(0x8583)})
        self.counts = counts
        self.conversion_s = conversion_s
        self.now = 0.0
        self.converting: tuple[float, int] | None = None  # (done at, result)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        super().write_i2c_block_data(addr, reg, data)
        config = int.from_bytes(bytes(data), "big")
        if reg == 0x01 and config & 0x8000:
            mux = (config >> 12) & 0b111
            assert mux >= 0b100, "only single-ended inputs are modelled"
            self.converting = (self.now + self.conversion_s, self.counts[mux - 0b100])
            self.regs[(addr, 0x01)] = be16(config & 0x7FFF)

    def read_i2c_block_data(self, addr: int, reg: int, n: int) -> list[int]:
        if self.converting is not None and self.now >= self.converting[0]:
            config = int.from_bytes(self.regs[(addr, 0x01)], "big")
            self.regs[(addr, 0x00)] = be16(self.converting[1])
            self.regs[(addr, 0x01)] = be16(config | 0x8000)
            self.converting = None
        return super().read_i2c_block_data(addr, reg, n)


def ads1115(fake: FakeADS1115) -> ADS1115:
    return ADS1115(make_bus(fake), sleep=fake.sleep)


@pytest.mark.parametrize(("channel", "word"), [(0, 0xC383), (1, 0xD383), (2, 0xE383), (3, 0xF383)])
def test_ads1115_config_word(channel: int, word: int) -> None:
    # 0xC383 is OS=1, MUX=100 (AIN0 to GND), PGA=001 (+/-4.096 V), MODE=1
    # (single-shot), DR=100 (128 SPS), COMP_QUE=11; each later input adds 1 to MUX.
    assert config_word(channel) == word
    fake = FakeADS1115([0, 0, 0, 0])
    ads1115(fake).read_volts(channel)
    assert fake.writes == [(0x48, 0x01, list(be16(word)))]


def test_ads1115_reads_the_input_asked_for() -> None:
    adc = ads1115(FakeADS1115([8000, 16000, 24000, 32000]))
    # 125 uV a count at +/-4.096 V full scale.
    assert [adc.read_volts(ch) for ch in (2, 0, 3, 1)] == pytest.approx([3.0, 1.0, 4.0, 2.0])


@pytest.mark.parametrize(
    ("counts", "volts"),
    [(0, 0.0), (20800, 2.6), (32767, 4.095875), (-1, -0.000125), (-32768, -4.096)],
)
def test_ads1115_counts_to_volts(counts: int, volts: float) -> None:
    # Two's complement: offset can put a grounded input a count or two below zero.
    assert ads1115(FakeADS1115([counts, 0, 0, 0])).read_volts(0) == pytest.approx(volts)


@pytest.mark.parametrize("conversion_s", [1 / 128, 0.045])
def test_ads1115_waits_for_the_conversion(conversion_s: float) -> None:
    fake = FakeADS1115([8000, 0, 0, 0], conversion_s=conversion_s)
    fake.regs[(0x48, 0x00)] = be16(-8000)  # an old result, there until the new one lands
    assert ads1115(fake).read_volts(0) == pytest.approx(1.0)


def test_ads1115_gives_up_on_a_conversion_after_50_ms() -> None:
    fake = FakeADS1115([8000, 0, 0, 0], conversion_s=1.0)
    with pytest.raises(SensorError, match="0x48: AIN0 conversion not done after 50 ms"):
        ads1115(fake).read_volts(0)
    assert fake.now == pytest.approx(0.05, abs=0.002)


@pytest.mark.parametrize("channel", [-1, 4])
def test_ads1115_has_four_inputs(channel: int) -> None:
    fake = FakeADS1115([0, 0, 0, 0])
    with pytest.raises(ValueError):
        ads1115(fake).read_volts(channel)
    assert fake.writes == []


def rf_power(fwd: int, ref: int, **kwargs: Any) -> RFPower:
    """An RFPower whose forward and reflected detectors convert to ``fwd`` and ``ref`` counts."""
    return RFPower("rf", ads1115(FakeADS1115([fwd, ref, 0, 0])), **kwargs)


def test_rf_power_and_swr() -> None:
    # 2.6 V from the forward detector is 104 dB above its -84 dBm intercept at
    # 25 mV/dB: +20 dBm, so 50 dBm (100 W) on the line past the 30 dB coupler.
    # 2.25 V from the reflected one is 36 dBm (3.98 W), 14 dB down: gamma is
    # 10^(-14/20) = 0.1995 and SWR 1.1995 / 0.8005 = 1.50.
    rf = rf_power(20800, 18000)
    assert (rf.kind, rf.fast) == ("reading", True)
    assert rf.read() == {"fwd_w": 100.0, "ref_w": 4.0, "rl_db": 14.0, "swr": 1.5}


def test_rf_power_with_its_own_calibration_and_channels() -> None:
    # 20 mV/dB from -90 dBm behind 40 dB of coupler and pad: 2.0 V is +10 dBm at
    # the detector and 50 dBm (100 W) on the line, 1.6 V is 30 dBm (1 W). A 20 dB
    # return loss is gamma 0.1, SWR 1.1 / 0.9 = 1.22.
    rf = RFPower(
        "rf",
        ads1115(FakeADS1115([0, 0, 12800, 16000])),
        forward_channel=3,
        reflected_channel=2,
        slope_mv_per_db=20.0,
        intercept_dbm=-90.0,
        coupling_db=40.0,
    )
    assert rf.read() == {"fwd_w": 100.0, "ref_w": 1.0, "rl_db": 20.0, "swr": 1.22}


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        # Everything comes back, as from an open or shorted line.
        (20800, {"fwd_w": 100.0, "ref_w": 100.0, "rl_db": 0.0, "swr": 99.0}),
        # More back than went out, which only calibration error can show.
        (21000, {"fwd_w": 100.0, "ref_w": 125.9, "rl_db": -1.0, "swr": 99.0}),
        # 0.1 dB return loss is SWR 174.
        (20780, {"fwd_w": 100.0, "ref_w": 97.7, "rl_db": 0.1, "swr": 99.0}),
    ],
)
def test_rf_swr_tops_out_at_99(ref: int, expected: dict[str, Value]) -> None:
    assert rf_power(20800, ref).read() == expected


def test_rf_reads_nothing_while_the_transmitter_is_unkeyed() -> None:
    # Both detectors at their floor, about -75 dBm (0.225 V): -45 dBm on the line.
    assert rf_power(1800, 1800).read() == {}
    # Either side of the 1 W default: 29 dBm (0.79 W) and 31 dBm (1.26 W).
    assert rf_power(16600, 1800).read() == {}
    assert rf_power(17000, 1800).read() == {"fwd_w": 1.3, "ref_w": 0.0, "rl_db": 76.0, "swr": 1.0}
    assert rf_power(20800, 18000, min_forward_w=200.0).read() == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slope_mv_per_db": 0.0},
        {"slope_mv_per_db": -25.0},
        {"coupling_db": -30.0},
        {"min_forward_w": 0.0},
        {"forward_channel": 1, "reflected_channel": 1},
        {"reflected_channel": 4},
    ],
)
def test_rf_rejects_bad_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        rf_power(0, 0, **kwargs)


def test_sim_rf_reads_only_while_keyed() -> None:
    now = [0.0]
    tx = sim.rf(forward_w=50.0, swr=1.3, duty=0.3, clock=lambda: now[0])
    assert (tx.kind, tx.fast) == ("reading", True)
    for t in (0.0, 17.0, 61.0, 125.0):  # keyed for the first 18 s of every minute
        now[0] = t
        r = tx.read()
        # SWR 1.3 is gamma 0.3 / 2.3 = 0.130: 17.7 dB return loss, 0.85 W back of 50.
        assert r["fwd_w"] == pytest.approx(50.0, abs=1.5)
        assert r["ref_w"] == pytest.approx(0.85, abs=0.1)
        assert r["rl_db"] == pytest.approx(17.7, abs=0.5)
        assert r["swr"] == pytest.approx(1.3, abs=0.02)
    for t in (18.5, 30.0, 59.0, 100.0):
        now[0] = t
        assert tx.read() == {}
    # A broken sensor shows up whether or not the transmitter is keyed.
    tx.fail_reads = 1
    with pytest.raises(SensorError):
        tx.read()
    with pytest.raises(ValueError):
        sim.rf(swr=1.0)


def test_sim_rf_noise_comes_from_the_seed() -> None:
    def run(seed: int) -> list[dict[str, Value]]:
        now = [0.0]
        tx = sim.rf(seed=seed, duty=1.0, clock=lambda: now[0])
        readings: list[dict[str, Value]] = []
        for t in range(20):
            now[0] = float(t)
            readings.append(tx.read())
        return readings

    assert run(3) == run(3)
    assert run(3) != run(4)
    assert len({r["fwd_w"] for r in run(3)}) > 1
