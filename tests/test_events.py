from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sitemon import Event, PublicKey, SigningKey, canonical_json
from sitemon._duration import human_duration, parse_duration


def ev(key: SigningKey, kind: str = "reading", data: dict[str, Any] | None = None) -> Event:
    return Event.create(
        site="s1",
        seq=1,
        boot="b",
        kind=kind,
        source="batt",
        data={"v": 13.4} if data is None else data,
    ).signed(key)


def test_sign_and_verify(key: SigningKey) -> None:
    e = ev(key)
    assert e.verify(key.public_key())
    assert not e.verify(SigningKey.generate().public_key())


def test_tampering_is_detected(key: SigningKey) -> None:
    e = ev(key)
    d = e.to_dict()
    d["data"]["v"] = 14.0
    assert not Event.from_dict(d).verify(key.public_key())
    d = e.to_dict()
    d["seq"] = 2
    assert not Event.from_dict(d).verify(key.public_key())


def test_json_round_trip_still_verifies(key: SigningKey) -> None:
    e = ev(key, data={"v": 12.345, "open": True, "links": ["2000"], "note": "säure"})
    back = Event.from_json(json.dumps(json.loads(e.to_json())))
    assert back == e
    assert back.verify(key.public_key())


def test_type_changes_are_rejected(key: SigningKey) -> None:
    d = ev(key).to_dict()
    d["seq"] = "1"
    with pytest.raises(ValueError):
        Event.from_dict(d)


def test_unknown_kind_rejected(key: SigningKey) -> None:
    with pytest.raises(ValueError):
        ev(key, kind="relay")


def test_key_round_trips(key: SigningKey) -> None:
    assert SigningKey.from_hex(key.to_hex()).public_key() == key.public_key()
    assert PublicKey.from_b64(key.public_key().to_b64()) == key.public_key()
    assert key.to_hex() not in repr(key)
    with pytest.raises(ValueError):
        PublicKey.from_b64("AAAA")


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53), 2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=10,
)


@given(st.dictionaries(st.text(min_size=1), json_values, max_size=6))
def test_canonical_json_survives_a_round_trip(data: dict[str, object]) -> None:
    # The hub re-serialises what it parsed; the bytes must match what was signed.
    once = canonical_json(data)
    assert canonical_json(json.loads(once)) == once


def test_durations() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration(12) == 12
    bad_values: tuple[str | int, ...] = ("soon", "-5s", True)
    for bad in bad_values:
        with pytest.raises(ValueError):
            parse_duration(bad)
    assert human_duration(1200) == "20 min"
    assert human_duration(3 * 86400) == "3 days"
