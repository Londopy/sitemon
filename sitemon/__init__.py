"""Repeater site monitoring: an agent at each site, one hub watching them all::

from sitemon import Event, SigningKey

key = SigningKey.generate()
ev = Event.create(site="hilltop", seq=1, boot="b1", kind="reading",
                  source="batt", data={"v": 13.4}).signed(key)
assert ev.verify(key.public_key())
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from ._events import Event, PublicKey, SigningKey, canonical_json

try:
    __version__ = version("sitemon")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["Event", "PublicKey", "SigningKey", "canonical_json", "__version__"]
