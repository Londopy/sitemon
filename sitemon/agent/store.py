"""The site's event log: never-repeating sequence numbers, batched writes to
spare the SD card, and a per-hub cursor so every hub gets every event."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .._events import Event, SigningKey

# Numbers are reserved a block at a time, so a crash leaves a gap; the hub
# orders by seq and never waits for missing ones.
SEQ_BLOCK = 1000
_CURSOR = "acked:"


class EventLog:
    def __init__(
        self,
        path: str,
        site: str,
        key: SigningKey,
        *,
        hubs: list[str],
        max_events: int = 500_000,
    ):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.site = site
        self.key = key
        self.hubs = list(dict.fromkeys(hubs))
        self.max_events = max_events
        self.boot = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL is durable enough with WAL and syncs far less often.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, expires REAL NOT NULL)"
        )
        self._pending: list[Event] = []
        self.dropped = 0
        reserved = self._meta("seq_reserved")
        newest = self._db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        self._next_seq = max(reserved, newest) + 1
        self._reserved = self._next_seq - 1
        # A hub dropped from the config may have been holding events back.
        self._prune()

    def _meta(self, k: str) -> int:
        row = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return int(row[0]) if row else 0

    def _set_meta(self, k: str, v: int) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))

    def _take_seq(self) -> int:
        if self._next_seq > self._reserved:
            reserved = self._next_seq + SEQ_BLOCK - 1
            # FULL for this one write: if the bound rolled back on power loss, a restart
            # would reuse numbers the hub already has.
            self._db.execute("PRAGMA synchronous=FULL")
            try:
                self._set_meta("seq_reserved", reserved)  # autocommit
            finally:
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._reserved = reserved
        seq = self._next_seq
        self._next_seq += 1
        return seq

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            # SQLite rolls back by itself after some errors, hence the check.
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def append(self, kind: str, source: str, data: dict[str, Any]) -> Event:
        """Create, sign and buffer an event. It reaches disk on the next flush."""
        with self._lock:
            ev = Event.create(
                site=self.site,
                seq=self._take_seq(),
                boot=self.boot,
                kind=kind,
                source=source,
                data=data,
            ).signed(self.key)
            self._pending.append(ev)
            return ev

    def flush(self) -> int:
        """Commit buffered events in one transaction. Returns how many.

        If the write fails, the batch goes back in front of the buffer for the
        next flush and the error is raised: one bad write loses nothing.
        """
        with self._lock:
            if not self._pending:
                return 0
            batch, self._pending = self._pending, []
            try:
                with self._transaction():
                    self._db.executemany(
                        "INSERT OR IGNORE INTO events (seq, body) VALUES (?, ?)",
                        [(e.seq, e.to_json()) for e in batch],
                    )
                    dropped = self._enforce_cap()
            except BaseException:
                self._pending[:0] = batch
                # Cap the buffer too, or a dead card takes the Pi's memory with it.
                excess = len(self._pending) - self.max_events
                if excess > 0:
                    del self._pending[:excess]
                    self.dropped += excess
                raise
            self.dropped += dropped
            return len(batch)

    def _enforce_cap(self) -> int:
        # A site cut off for months must not fill the card; the heartbeat reports drops.
        n = self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        excess: int = n - self.max_events
        if excess <= 0:
            return 0
        self._db.execute(
            "DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?)",
            (excess,),
        )
        return excess

    def _cursor(self, hub: str) -> int:
        return self._meta(_CURSOR + hub)

    def _floor(self) -> int:
        return min((self._cursor(h) for h in self.hubs), default=0)

    def _prune(self) -> None:
        if self.hubs:
            self._db.execute("DELETE FROM events WHERE seq <= ?", (self._floor(),))

    def unsent(self, hub: str, limit: int = 500) -> list[Event]:
        """The oldest events ``hub`` hasn't confirmed yet, at most ``limit``."""
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (self._cursor(hub), limit),
            ).fetchall()
        return [Event.from_json(r[0]) for r in rows]

    def ack(self, hub: str, through_seq: int) -> None:
        """``hub`` has stored everything up to and including ``through_seq``.

        A cursor only moves forward: a late or repeated answer can't take back
        what the hub already confirmed. Events that every configured hub now
        has are deleted in the same transaction.
        """
        with self._lock:
            if through_seq <= self._cursor(hub):
                return
            with self._transaction():
                self._set_meta(_CURSOR + hub, through_seq)
                self._prune()

    def lag(self, hub: str) -> int:
        """Events ``hub`` hasn't confirmed yet, counting ones not flushed."""
        with self._lock:
            return self._count_after(self._cursor(hub))

    def depth(self) -> int:
        """Events some configured hub still lacks, counting ones not flushed."""
        with self._lock:
            return self._count_after(self._floor())

    def _count_after(self, seq: int) -> int:
        n: int = self._db.execute("SELECT COUNT(*) FROM events WHERE seq > ?", (seq,)).fetchone()[0]
        return n + len(self._pending)

    def first_use(self, nonce: str, expires: float) -> bool:
        """Record a command's nonce; False if it has been used before. Kept on
        disk, so a restart can't be used to replay a command."""
        with self._lock:
            # Kept a month, not until expiry: a clock stepped back must not
            # make an old command look new.
            self._db.execute("DELETE FROM nonces WHERE expires < ?", (time.time() - 30 * 86400,))
            # FULL, like the seq reservation: a power cut must not undo this,
            # or the hub's resend would run the command a second time.
            self._db.execute("PRAGMA synchronous=FULL")
            try:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO nonces (nonce, expires) VALUES (?, ?)", (nonce, expires)
                )
            finally:
                self._db.execute("PRAGMA synchronous=NORMAL")
            return cur.rowcount == 1

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._db.close()
