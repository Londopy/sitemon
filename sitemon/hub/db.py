"""The hub's SQLite database. Metrics are named ``<source>.<key>`` (``batt.v``),
and ``t`` is the hub's estimate of when a reading was taken, not the site's
wall clock."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    site TEXT NOT NULL, seq INTEGER NOT NULL, boot TEXT NOT NULL, mono_ns INTEGER NOT NULL,
    wall REAL NOT NULL, t REAL NOT NULL, recv REAL NOT NULL, kind TEXT NOT NULL,
    source TEXT NOT NULL, data TEXT NOT NULL, sig TEXT NOT NULL,
    PRIMARY KEY (site, boot, seq)
);
CREATE INDEX IF NOT EXISTS events_t ON events (site, t);
CREATE INDEX IF NOT EXISTS events_recv ON events (recv);
CREATE TABLE IF NOT EXISTS points (
    site TEXT NOT NULL, metric TEXT NOT NULL, t REAL NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS points_idx ON points (site, metric, t);
CREATE INDEX IF NOT EXISTS points_t ON points (t);
CREATE TABLE IF NOT EXISTS latest (
    site TEXT NOT NULL, metric TEXT NOT NULL, t REAL NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (site, metric)
);
CREATE TABLE IF NOT EXISTS boots (
    site TEXT NOT NULL, boot TEXT NOT NULL, offset REAL NOT NULL,
    PRIMARY KEY (site, boot)
);
CREATE TABLE IF NOT EXISTS contact (
    site TEXT PRIMARY KEY, last_recv REAL NOT NULL, last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, site TEXT NOT NULL, rule TEXT NOT NULL, severity TEXT NOT NULL,
    opened REAL NOT NULL, updated REAL NOT NULL, resolved REAL, message TEXT NOT NULL,
    notified REAL NOT NULL, notified_severity TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_open ON alerts (site, rule) WHERE resolved IS NULL;
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, channel TEXT NOT NULL, created REAL NOT NULL, next_try REAL NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0, severity TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, done REAL, last_error TEXT, target TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (done, next_try);
CREATE TABLE IF NOT EXISTS rejected (
    site TEXT NOT NULL, seq INTEGER, recv REAL NOT NULL, reason TEXT NOT NULL, raw TEXT NOT NULL,
    digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS rejected_recv ON rejected (site, recv);
CREATE UNIQUE INDEX IF NOT EXISTS rejected_once ON rejected (site, digest);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
    nonce TEXT PRIMARY KEY, site TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_site ON commands (site, status);
"""


@dataclass
class Alert:
    id: int
    site: str
    rule: str
    severity: str
    opened: float
    updated: float
    resolved: float | None
    message: str
    notified: float
    notified_severity: str


class HubDB:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL: once the hub acknowledges an upload the site
        # deletes it, so a commit that rolls back on power loss would lose
        # data for good. One fsync per upload is cheap at this volume.
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        # Hubs created by 0.1.0 have an outbox without the per-recipient column.
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(outbox)")}
        if "target" not in cols:
            self._db.execute("ALTER TABLE outbox ADD COLUMN target TEXT NOT NULL DEFAULT ''")
        self.lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                # Inside the try: a COMMIT that fails (disk full, I/O error)
                # must roll back too, or the connection stays mid-transaction
                # and every later tx() fails.
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self._db.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock:
            row: sqlite3.Row | None = self._db.execute(sql, args).fetchone()
            return row

    def x(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self.lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def get_meta(self, k: str) -> str | None:
        row = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return None if row is None else str(row["v"])

    def set_meta(self, k: str, v: str) -> None:
        self.x("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))

    def latest(self, site: str) -> dict[str, tuple[Any, float]]:
        return {
            r["metric"]: (json.loads(r["value"]), r["t"])
            for r in self.q("SELECT metric, value, t FROM latest WHERE site=?", (site,))
        }

    def latest_value(self, site: str, metric: str) -> tuple[Any, float] | None:
        r = self.q1("SELECT value, t FROM latest WHERE site=? AND metric=?", (site, metric))
        return None if r is None else (json.loads(r["value"]), r["t"])

    def last_contact(self, site: str) -> float | None:
        r = self.q1("SELECT last_recv FROM contact WHERE site=?", (site,))
        return None if r is None else float(r["last_recv"])

    def bad_since(
        self, site: str, metric: str, good_sql: str, args: tuple[Any, ...], until: float
    ) -> float | None:
        """When the current bad run of ``metric`` began: the first point after the
        last one matching ``good_sql`` (an SQL condition on ``value``)."""
        last_good = self.q1(
            f"SELECT t FROM points WHERE site=? AND metric=? AND t<=? AND ({good_sql}) "
            "ORDER BY t DESC LIMIT 1",
            (site, metric, until, *args),
        )
        row = self.q1(
            "SELECT MIN(t) AS t FROM points WHERE site=? AND metric=? AND t>? AND t<=?",
            (site, metric, last_good["t"] if last_good else float("-inf"), until),
        )
        return None if row is None or row["t"] is None else float(row["t"])

    def point_before(self, site: str, metric: str, t: float) -> tuple[float, float] | None:
        row = self.q1(
            "SELECT t, value FROM points WHERE site=? AND metric=? AND t<? ORDER BY t DESC LIMIT 1",
            (site, metric, t),
        )
        return None if row is None else (float(row["t"]), float(row["value"]))

    def newest_event(self, site: str) -> float | None:
        row = self.q1("SELECT MAX(t) AS t FROM events WHERE site=?", (site,))
        return None if row is None or row["t"] is None else float(row["t"])

    def rejected_since(self, site: str, t: float) -> list[str]:
        return [
            r["reason"]
            for r in self.q(
                "SELECT reason FROM rejected WHERE site=? AND recv>=? ORDER BY recv", (site, t)
            )
        ]

    def mean(self, site: str, metric: str, t0: float, t1: float) -> tuple[float | None, int]:
        r = self.q1(
            "SELECT AVG(value) AS m, COUNT(*) AS n FROM points "
            "WHERE site=? AND metric=? AND t>=? AND t<=?",
            (site, metric, t0, t1),
        )
        assert r is not None
        return (None if r["m"] is None else float(r["m"])), int(r["n"])

    def series(
        self, site: str, metric: str, t0: float, t1: float, max_points: int = 500
    ) -> list[tuple[float, float]]:
        """Points in [t0, t1], averaged into at most ``max_points`` buckets."""
        # Bucket over the span that actually has data, so a site added an hour
        # ago gets an hour-wide chart rather than one dot in a 24 h window.
        first = self.q1(
            "SELECT MIN(t) AS t FROM points WHERE site=? AND metric=? AND t>=? AND t<=?",
            (site, metric, t0, t1),
        )
        if first is not None and first["t"] is not None:
            t0 = max(t0, float(first["t"]))
        bucket = max((t1 - t0) / max_points, 1.0)
        rows = self.q(
            "SELECT CAST((t - ?) / ? AS INTEGER) AS b, AVG(t) AS t, AVG(value) AS v "
            "FROM points WHERE site=? AND metric=? AND t>=? AND t<=? GROUP BY b ORDER BY b",
            (t0, bucket, site, metric, t0, t1),
        )
        return [(round(r["t"], 1), round(r["v"], 4)) for r in rows]

    def raw_points(self, site: str, metric: str, t0: float, t1: float) -> list[tuple[float, float]]:
        rows = self.q(
            "SELECT t, value FROM points WHERE site=? AND metric=? AND t>=? AND t<=? ORDER BY t",
            (site, metric, t0, t1),
        )
        return [(r["t"], r["value"]) for r in rows]

    def metrics(self, site: str) -> list[str]:
        return [
            r["metric"]
            for r in self.q(
                "SELECT DISTINCT metric FROM latest WHERE site=? ORDER BY metric", (site,)
            )
        ]

    def has_history(self, site: str, metric: str) -> bool:
        """Whether there are charted points for this metric. Not ``latest``: a
        state snapshot can drop a key from latest while its history stays."""
        return (
            self.q1("SELECT 1 FROM points WHERE site=? AND metric=? LIMIT 1", (site, metric))
            is not None
        )

    @staticmethod
    def _alert(r: sqlite3.Row) -> Alert:
        return Alert(**{k: r[k] for k in r.keys()})

    def open_alert(self, site: str, rule: str) -> Alert | None:
        r = self.q1(
            "SELECT * FROM alerts WHERE site=? AND rule=? AND resolved IS NULL", (site, rule)
        )
        return None if r is None else self._alert(r)

    def open_alerts(self, site: str | None = None) -> list[Alert]:
        if site is None:
            rows = self.q("SELECT * FROM alerts WHERE resolved IS NULL ORDER BY opened")
        else:
            rows = self.q(
                "SELECT * FROM alerts WHERE site=? AND resolved IS NULL ORDER BY opened", (site,)
            )
        return [self._alert(r) for r in rows]

    def alerts_since(self, t: float) -> list[Alert]:
        return [
            self._alert(r)
            for r in self.q("SELECT * FROM alerts WHERE opened>=? ORDER BY opened", (t,))
        ]

    def prune(self, before: float, chunk: int = 5000) -> int:
        """Delete history older than ``before``, a chunk at a time.

        Each chunk is its own short transaction and the lock is released in
        between, so ingest and the rules loop keep running while a year of
        old rows goes. Returns how many rows went.
        """
        targets = [
            ("points", "t < ?"),
            ("events", "recv < ?"),
            ("alerts", "resolved IS NOT NULL AND resolved < ?"),
            ("outbox", "done IS NOT NULL AND done < ?"),
            ("rejected", "recv < ?"),
            ("commands", "created < ?"),
        ]
        total = 0
        for table, where in targets:
            while True:
                with self.tx() as db:
                    n = db.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} WHERE {where} LIMIT ?)",
                        (before, chunk),
                    ).rowcount
                total += n
                if n < chunk:
                    break
        return total
