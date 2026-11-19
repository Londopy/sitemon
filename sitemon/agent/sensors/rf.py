"""Forward and reflected power from a directional coupler, with an AD8307 log
detector on each coupled port read through an ADS1115.

It shows trends, not calibrated absolute power: the slope and intercept come
from comparing its readings with a wattmeter.
"""

from __future__ import annotations

from typing import Literal

from .ads1115 import ADS1115
from .base import Value


def vswr(return_loss_db: float) -> float:
    """SWR for a return loss in dB, capped at 99."""
    gamma = 10 ** (-return_loss_db / 20)
    # Reflected at or above forward: an open or short, give or take calibration.
    if gamma >= 1:
        return 99.0
    return min((1 + gamma) / (1 - gamma), 99.0)


def _watts(dbm: float) -> float:
    return 10 ** ((dbm - 30) / 10)


class RFPower:
    """Feedline forward and reflected power, return loss and SWR.

    ``coupling_db`` is the coupler's coupling plus any attenuator in front of
    the detectors. While forward power is under ``min_forward_w`` the
    transmitter is unkeyed, and ``read`` returns ``{}``: no reading, not a fault.
    """

    kind: Literal["reading", "state"] = "reading"
    # A transmission can be shorter than the sample interval, so the agent
    # reads this on its 1 s loop.
    fast = True

    def __init__(
        self,
        name: str,
        adc: ADS1115,
        *,
        forward_channel: int = 0,
        reflected_channel: int = 1,
        slope_mv_per_db: float = 25.0,
        intercept_dbm: float = -84.0,
        coupling_db: float = 30.0,
        min_forward_w: float = 1.0,
    ) -> None:
        if slope_mv_per_db <= 0:
            raise ValueError(f"{name}: slope_mv_per_db must be positive, not {slope_mv_per_db!r}")
        if coupling_db < 0:
            raise ValueError(f"{name}: coupling_db must be 0 or more, not {coupling_db!r}")
        if min_forward_w <= 0:
            raise ValueError(f"{name}: min_forward_w must be positive, not {min_forward_w!r}")
        # Checked here rather than left to the ADC: read() may only raise SensorError.
        for channel in (forward_channel, reflected_channel):
            if channel not in range(4):
                raise ValueError(f"{name}: ADS1115 channels are 0-3, not {channel!r}")
        if forward_channel == reflected_channel:
            raise ValueError(f"{name}: forward and reflected are both on channel {forward_channel}")
        self.name = name
        self.adc = adc
        self.forward_channel = forward_channel
        self.reflected_channel = reflected_channel
        self.slope_mv_per_db = slope_mv_per_db
        self.intercept_dbm = intercept_dbm
        self.coupling_db = coupling_db
        self.min_forward_w = min_forward_w

    def read(self) -> dict[str, Value]:
        fwd_dbm = self._line_dbm(self.forward_channel)
        fwd_w = _watts(fwd_dbm)
        if fwd_w < self.min_forward_w:
            return {}
        ref_dbm = self._line_dbm(self.reflected_channel)
        rl_db = fwd_dbm - ref_dbm
        return {
            "fwd_w": round(fwd_w, 1),
            "ref_w": round(_watts(ref_dbm), 1),
            "rl_db": round(rl_db, 1),
            "swr": round(vswr(rl_db), 2),
        }

    def close(self) -> None:
        # The bus is shared; whoever opened it closes it.
        pass

    def _line_dbm(self, channel: int) -> float:
        # The AD8307 puts out slope_mv_per_db for every dB above intercept_dbm.
        volts = self.adc.read_volts(channel)
        return volts / (self.slope_mv_per_db / 1000) + self.intercept_dbm + self.coupling_db
