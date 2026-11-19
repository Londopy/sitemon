"""Signed commands to a site: relay commands from an operator, announcements
from the hub. Each names its site, carries a one-time nonce and a short
expiry, and is Ed25519-signed, so a captured command can't be replayed or
sent to another site."""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ._events import PublicKey, SigningKey, canonical_json

KINDS = ("relay", "announce")
# Commands are for now, not later: a long-lived one is a replay waiting to happen.
MAX_LIFETIME = 900.0
CLOCK_SKEW = 60.0
_NONCE = re.compile(r"[0-9a-f]{32}")


def speakable(text: str) -> str:
    """Plain words for text-to-speech: no symbols to read out, no leading
    dash a TTS program could take for an option, and short."""
    words = re.sub(r"[^A-Za-z0-9 .,:%]+", " ", text)
    return re.sub(r"\s+", " ", words).strip()[:200]


@dataclass(frozen=True)
class Command:
    site: str
    kind: str
    nonce: str
    issued: float
    expires: float
    args: dict[str, Any] = field(default_factory=dict)
    sig: str = ""

    @classmethod
    def create(
        cls,
        site: str,
        kind: str,
        args: dict[str, Any],
        *,
        lifetime: float = 120.0,
        now: float | None = None,
    ) -> Command:
        if kind not in KINDS:
            raise ValueError(f"unknown command kind {kind!r}")
        if not 0 < lifetime <= MAX_LIFETIME:
            raise ValueError(f"lifetime must be 1-{int(MAX_LIFETIME)} s")
        now = time.time() if now is None else now
        return cls(
            site=site,
            kind=kind,
            nonce=secrets.token_hex(16),
            issued=round(now, 3),
            expires=round(now + lifetime, 3),
            args=args,
        )

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        del d["sig"]
        return d

    def signed(self, key: SigningKey) -> Command:
        return replace(self, sig=key.sign(canonical_json(self.body())))

    def signer(self, keys: list[PublicKey]) -> PublicKey | None:
        """The key in ``keys`` that signed this, if any."""
        if not self.sig:
            return None
        try:
            msg = canonical_json(self.body())
        except ValueError:  # text that can't be encoded (a lone surrogate) can't be signed
            return None
        return next((k for k in keys if k.verify(self.sig, msg)), None)

    def problem(self, now: float) -> str | None:
        """Why this command can't be run at ``now``, or None if it can."""
        if self.issued - CLOCK_SKEW > now:
            return "issued in the future (check the clocks)"
        if self.expires < now:
            return "expired"
        if self.expires - self.issued > MAX_LIFETIME:
            return "lifetime too long"
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> Command:
        if not isinstance(d, dict):
            raise ValueError("command must be an object")
        try:
            cmd = cls(
                site=str(d["site"]),
                kind=str(d["kind"]),
                nonce=str(d["nonce"]),
                issued=float(d["issued"]),
                expires=float(d["expires"]),
                args=dict(d.get("args", {})),
                sig=str(d.get("sig", "")),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            raise ValueError(f"malformed command: {e}") from e
        if cmd.kind not in KINDS:
            raise ValueError(f"unknown command kind {cmd.kind!r}")
        if not _NONCE.fullmatch(cmd.nonce):
            raise ValueError("malformed command nonce")
        if not isinstance(d["issued"], (int, float)) or not isinstance(d["expires"], (int, float)):
            raise ValueError("malformed command times")
        return cmd
