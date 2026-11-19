"""Accepting a site's upload.

The envelope's signature covers every event, so a bad envelope is refused
outright, while a bad event inside a genuine one is set aside and acknowledged
so it can't block the site's queue. Events are placed in time from the agent's
monotonic clock plus a per-boot offset learned from each upload, not from the
site's wall clock.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .._events import Event, PublicKey, canonical_json, envelope_body
from .db import HubDB

MAX_REJECTED = 1000

#: Before this, a site's wall clock is certainly unset (no RTC, no NTP yet).
SANE_WALL = 1_577_836_800.0  # 2020-01-01


class IngestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class IngestResult:
    site: str
    stored: list[tuple[Event, float]] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    acked_through: int = 0
    # Counter went backwards under a new boot: SD card replaced or image restored.
    counter_reset: bool = False


def _scalar_points(ev: Event) -> list[tuple[str, float]]:
    out = []
    for k, v in ev.data.items():
        if isinstance(v, bool):
            out.append((f"{ev.source}.{k}", 1.0 if v else 0.0))
        elif isinstance(v, (int, float)):
            out.append((f"{ev.source}.{k}", float(v)))
    return out


def _like_prefix(source: str) -> str:
    """A LIKE pattern for ``<source>.<anything>``, with the source taken literally."""
    esc = source.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + ".%"


def _finite(v: Any) -> bool:
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return -(2**63) <= v < 2**63  # fits SQLite, and converts to float
    if isinstance(v, dict):
        return all(_finite(x) for x in v.values())
    if isinstance(v, list):
        return all(_finite(x) for x in v)
    return True


def _check_event(raw: Any, site: str, site_keys: list[PublicKey]) -> Event:
    """Parse and verify one event, or raise ValueError saying why not."""
    ev = Event.from_dict(raw)
    if ev.site != site:
        raise ValueError(f"claims site {ev.site!r}")
    if not any(ev.verify(k) for k in site_keys):
        raise ValueError("bad signature")
    if not _finite(ev.data):
        raise ValueError("non-finite or out-of-range number in data")
    return ev


def ingest(
    db: HubDB, keys: dict[str, list[PublicKey]], env: Any, *, now: float | None = None
) -> IngestResult:
    recv = time.time() if now is None else now
    if not isinstance(env, dict):
        raise IngestError(400, "upload must be a JSON object")
    try:
        site = str(env["site"])
        boot = str(env["boot"])
        mono_now = int(env["mono_ns"])
        raw_events = env["events"]
        sig = str(env["sig"])
        if not isinstance(raw_events, list):
            raise TypeError("events must be a list")
        body = envelope_body(env)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise IngestError(400, f"malformed upload: {e}") from e

    site_keys = keys.get(site)
    if not site_keys:
        raise IngestError(403, f"unknown site {site!r}")
    if not any(k.verify(sig, canonical_json(body)) for k in site_keys):
        raise IngestError(403, f"bad upload signature from {site!r}")

    result = IngestResult(site=site)
    events: list[Event] = []
    rejected: list[tuple[int | None, str, str]] = []
    for i, raw in enumerate(raw_events):
        raw_seq = raw.get("seq") if isinstance(raw, dict) else None
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else None
        try:
            events.append(_check_event(raw, site, site_keys))
        except (ValueError, TypeError, OverflowError) as e:
            reason = f"event {i} (seq {raw_seq}): {e}"
            rejected.append(
                (
                    seq if seq is not None and 0 <= seq < 2**63 else None,
                    reason,
                    json.dumps(raw, default=str)[:4000],
                )
            )
            result.quarantined.append(reason)
    events.sort(key=lambda e: e.seq)
    # Only verified seqs count, or one forged 10**12 would poison reset detection.
    max_seq = max((e.seq for e in events), default=0)

    with db.tx() as c:
        offset = recv - mono_now / 1e9
        row = c.execute("SELECT offset FROM boots WHERE site=? AND boot=?", (site, boot)).fetchone()
        if row is None or offset < row["offset"]:
            c.execute(
                "INSERT OR REPLACE INTO boots (site, boot, offset) VALUES (?, ?, ?)",
                (site, boot, offset),
            )
        offsets = {
            r["boot"]: r["offset"]
            for r in c.execute("SELECT boot, offset FROM boots WHERE site=?", (site,))
        }
        prev = c.execute("SELECT last_seq FROM contact WHERE site=?", (site,)).fetchone()
        prev_seq = int(prev["last_seq"]) if prev else 0
        known_boots = {
            b
            for b in {e.boot for e in events}
            if c.execute(
                "SELECT 1 FROM events WHERE site=? AND boot=? LIMIT 1", (site, b)
            ).fetchone()
        }
        for rseq, reason, raw_json in rejected:
            # De-duplicated and capped, so replaying an upload can't fill the disk.
            c.execute(
                "INSERT OR IGNORE INTO rejected (site, seq, recv, reason, raw, digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (site, rseq, recv, reason, raw_json, hashlib.sha256(raw_json.encode()).hexdigest()),
            )
        if rejected:
            c.execute(
                "DELETE FROM rejected WHERE site=? AND rowid NOT IN "
                "(SELECT rowid FROM rejected WHERE site=? ORDER BY recv DESC LIMIT ?)",
                (site, site, MAX_REJECTED),
            )
        reset_boots: set[str] = set()
        for ev in events:
            if ev.boot not in known_boots and ev.seq <= prev_seq:
                result.counter_reset = True
                reset_boots.add(ev.boot)
            known_boots.add(ev.boot)
            if ev.boot in offsets:
                t = ev.mono_ns / 1e9 + offsets[ev.boot]
            elif SANE_WALL <= ev.wall <= recv + 300:
                t = ev.wall
            else:
                t = recv
            t = min(t, recv)  # nothing is from the future
            # Keyed by boot too, so a restarted counter isn't dropped as duplicates.
            cur = c.execute(
                "INSERT OR IGNORE INTO events (site, seq, boot, mono_ns, wall, t, recv, kind, "
                "source, data, sig) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    site,
                    ev.seq,
                    ev.boot,
                    ev.mono_ns,
                    ev.wall,
                    t,
                    recv,
                    ev.kind,
                    ev.source,
                    json.dumps(ev.data, sort_keys=True),
                    ev.sig,
                ),
            )
            if cur.rowcount == 0:
                continue  # already have it: a retried upload
            result.stored.append((ev, t))
            if ev.kind in ("reading", "state", "heartbeat"):
                c.executemany(
                    "INSERT INTO points (site, metric, t, value) VALUES (?, ?, ?, ?)",
                    [(site, m, t, v) for m, v in _scalar_points(ev)],
                )
                updates = [(f"{ev.source}.{k}", v) for k, v in ev.data.items()]
            else:  # fault
                updates = [
                    (
                        f"{ev.source}.fault",
                        None if ev.data.get("ok") else ev.data.get("error", "failing"),
                    )
                ]
            if ev.kind == "state":
                newest = c.execute(
                    "SELECT MAX(t) AS t FROM latest WHERE site=? AND metric LIKE ? ESCAPE '\\' "
                    "AND metric != ?",
                    (site, _like_prefix(ev.source), f"{ev.source}.fault"),
                ).fetchone()["t"]
                if newest is not None and newest > t:
                    # Older than what we have: history only, or it would revive dropped keys.
                    continue
            for metric, v in updates:
                # A late backlog must not overwrite a newer value.
                c.execute(
                    "INSERT INTO latest (site, metric, t, value) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (site, metric) DO UPDATE SET t=excluded.t, value=excluded.value "
                    "WHERE excluded.t >= latest.t",
                    (site, metric, t, json.dumps(v)),
                )
            if ev.kind == "state":
                # A state event is its source's whole picture: keys it dropped are gone.
                keep = [f"{ev.source}.{k}" for k in ev.data] + [f"{ev.source}.fault"]
                marks = ",".join("?" * len(keep))
                c.execute(
                    f"DELETE FROM latest WHERE site=? AND metric LIKE ? ESCAPE '\\' "
                    f"AND metric NOT IN ({marks}) AND t <= ?",
                    (site, _like_prefix(ev.source), *keep, t),
                )
        # Only new data counts as contact, so a replayed upload can't fake a live site.
        if result.stored:
            if reset_boots:
                # The old high-water mark belonged to the replaced card.
                new_seq = max(e.seq for e in events if e.boot in reset_boots)
            else:
                new_seq = max(max_seq, prev_seq)
            c.execute(
                "INSERT OR REPLACE INTO contact (site, last_recv, last_seq) VALUES (?, ?, ?)",
                (site, recv, new_seq),
            )
    # "last" is signed, so acking through it (quarantined events included) is
    # safe and stops a bad event being resent for ever.
    last = body.get("last")
    result.acked_through = last if isinstance(last, int) and not isinstance(last, bool) else max_seq
    return result
