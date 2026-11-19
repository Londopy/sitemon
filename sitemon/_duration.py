"""Duration parsing for config and wording for alert text."""

from __future__ import annotations

import re

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)?\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, None: 1}


def parse_duration(value: str | int | float) -> float:
    """``"30s"``, ``"5m"``, ``"7d"`` or a bare number of seconds -> seconds."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"negative duration: {value!r}")
        return float(value)
    m = _DURATION.match(value)
    if not m:
        raise ValueError(f"not a duration: {value!r} (use e.g. 30s, 5m, 2h, 7d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def human_duration(seconds: float) -> str:
    """Plain-language duration for alert text: ``"20 min"``, ``"3 h"``, ``"2 days"``."""
    s = int(seconds)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"
