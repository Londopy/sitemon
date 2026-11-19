"""Events and their Ed25519 signatures.

Each event carries a per-site ``seq`` that never repeats and the agent's
monotonic clock (``boot`` + ``mono_ns``), so the hub can de-duplicate and place
it in time without trusting the site's wall clock. Signed, never encrypted:
data over AREDN must stay readable (Part 97).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

KINDS = ("reading", "state", "fault", "heartbeat")


def canonical_json(obj: Any) -> bytes:
    """The exact bytes that get signed: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def envelope_body(env: dict[str, Any]) -> dict[str, Any]:
    """What an upload's signature covers, including a digest of every event:
    without it, a swapped event would be set aside and acked, and the real
    one deleted by the site."""
    evs = env.get("events", [])
    return {
        "site": env["site"],
        "boot": env["boot"],
        "mono_ns": env["mono_ns"],
        "wall": env["wall"],
        "count": len(evs),
        "first": evs[0]["seq"] if evs else None,
        "last": evs[-1]["seq"] if evs else None,
        "events_sha256": hashlib.sha256(canonical_json(evs)).hexdigest(),
    }


class PublicKey:
    """An Ed25519 public key, written as base64 in hub config."""

    def __init__(self, key: Ed25519PublicKey) -> None:
        self._key = key

    @classmethod
    def from_b64(cls, s: str) -> PublicKey:
        raw = _unb64(s.strip())
        if len(raw) != 32:
            raise ValueError("an Ed25519 public key is 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    def to_b64(self) -> str:
        return _b64(self._key.public_bytes(Encoding.Raw, PublicFormat.Raw))

    def verify(self, sig_b64: str, message: bytes) -> bool:
        try:
            self._key.verify(_unb64(sig_b64), message)
        except (InvalidSignature, ValueError):
            return False
        return True

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicKey) and other.to_b64() == self.to_b64()

    def __hash__(self) -> int:
        return hash(self.to_b64())

    def __repr__(self) -> str:
        return f"PublicKey({self.to_b64()!r})"


class SigningKey:
    """A site's Ed25519 private key. Stored as 64 hex characters in a 0600 file."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_hex(cls, s: str) -> SigningKey:
        raw = bytes.fromhex(s.strip())
        if len(raw) != 32:
            raise ValueError("an Ed25519 private key is 32 bytes (64 hex characters)")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @classmethod
    def load(cls, path: str) -> SigningKey:
        with open(path, encoding="ascii") as f:
            return cls.from_hex(f.read())

    def to_hex(self) -> str:
        return self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()

    def public_key(self) -> PublicKey:
        return PublicKey(self._key.public_key())

    def sign(self, message: bytes) -> str:
        return _b64(self._key.sign(message))

    def __repr__(self) -> str:  # never print key material
        return f"SigningKey(public={self.public_key().to_b64()!r})"


@dataclass(frozen=True)
class Event:
    site: str
    seq: int
    boot: str
    mono_ns: int
    wall: float
    kind: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)
    sig: str = ""

    @classmethod
    def create(
        cls,
        *,
        site: str,
        seq: int,
        boot: str,
        kind: str,
        source: str,
        data: dict[str, Any],
        mono_ns: int | None = None,
        wall: float | None = None,
    ) -> Event:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        return cls(
            site=site,
            seq=seq,
            boot=boot,
            mono_ns=time.monotonic_ns() if mono_ns is None else mono_ns,
            wall=round(time.time() if wall is None else wall, 3),
            kind=kind,
            source=source,
            data=data,
        )

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        del d["sig"]
        return d

    def signing_bytes(self) -> bytes:
        return canonical_json(self.body())

    def signed(self, key: SigningKey) -> Event:
        return replace(self, sig=key.sign(self.signing_bytes()))

    def verify(self, key: PublicKey) -> bool:
        return bool(self.sig) and key.verify(self.sig, self.signing_bytes())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        try:
            ev = cls(
                site=str(d["site"]),
                seq=int(d["seq"]),
                boot=str(d["boot"]),
                mono_ns=int(d["mono_ns"]),
                wall=float(d["wall"]),
                kind=str(d["kind"]),
                source=str(d["source"]),
                data=dict(d["data"]),
                sig=str(d.get("sig", "")),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            raise ValueError(f"malformed event: {e}") from e
        if ev.kind not in KINDS:
            raise ValueError(f"unknown event kind {ev.kind!r}")
        # The signature covers the parsed body, so "5" must not pass for 5.
        if any(not isinstance(d[k], t) or isinstance(d[k], bool) for k, t in _WIRE_TYPES.items()):
            raise ValueError("malformed event: field has the wrong type")
        # SQLite integers are signed 64-bit.
        if not (0 <= ev.seq < 2**63 and 0 <= ev.mono_ns < 2**63) or not math.isfinite(ev.wall):
            raise ValueError("malformed event: seq, mono_ns or wall out of range")
        return ev

    @classmethod
    def from_json(cls, s: str) -> Event:
        return cls.from_dict(json.loads(s))


_WIRE_TYPES: dict[str, type | tuple[type, ...]] = {
    "site": str,
    "seq": int,
    "boot": str,
    "mono_ns": int,
    "wall": (int, float),
    "kind": str,
    "source": str,
    "data": dict,
}
