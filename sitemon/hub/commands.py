"""The hub's queue of signed commands waiting for their site to collect them.

The hub only relays commands: relay commands are signed by an operator on
their own machine, so the hub can't forge one. It checks the signature anyway,
so nobody can fill the queue with junk.
"""

from __future__ import annotations

import json
from typing import Any

from .._commands import Command
from .db import HubDB

LIVE = ("queued", "sent")


class CommandError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def queue(db: HubDB, cmd: Command, now: float) -> None:
    if (why := cmd.problem(now)) is not None:
        raise CommandError(400, f"command {why}")
    with db.tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO commands (nonce, site, kind, body, created, expires, status, "
            "detail, updated) VALUES (?, ?, ?, ?, ?, ?, 'queued', '', ?)",
            (cmd.nonce, cmd.site, cmd.kind, json.dumps(cmd.to_dict()), now, cmd.expires, now),
        )
        if cur.rowcount == 0:
            raise CommandError(409, "a command with this nonce was already queued")


def collect(db: HubDB, site: str, now: float) -> list[dict[str, Any]]:
    """Live commands for ``site``, marked sent. A command stays live until the
    site reports a result or it expires, so a lost reply is simply resent; the
    site runs each nonce only once."""
    with db.tx() as c:
        c.execute(
            "UPDATE commands SET status='expired', updated=? "
            "WHERE status IN ('queued', 'sent') AND expires < ?",
            (now, now),
        )
        rows = c.execute(
            "SELECT nonce, body FROM commands WHERE site=? AND status IN ('queued', 'sent') "
            "ORDER BY created",
            (site,),
        ).fetchall()
        c.executemany(
            "UPDATE commands SET status='sent', updated=? WHERE nonce=?",
            [(now, r["nonce"]) for r in rows],
        )
    return [json.loads(r["body"]) for r in rows]


def status(db: HubDB, nonce: str) -> dict[str, Any] | None:
    r = db.q1("SELECT site, kind, status, detail, updated FROM commands WHERE nonce=?", (nonce,))
    return None if r is None else {k: r[k] for k in r.keys()}


def recent(db: HubDB, site: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.q(
        "SELECT kind, body, status, detail, created FROM commands WHERE site=? "
        "ORDER BY created DESC LIMIT ?",
        (site, limit),
    )
    return [
        {
            "kind": r["kind"],
            "args": json.loads(r["body"]).get("args", {}),
            "status": r["status"],
            "detail": r["detail"],
            "created": r["created"],
        }
        for r in rows
    ]


def last_announcement(db: HubDB, site: str) -> float | None:
    r = db.q1("SELECT MAX(created) AS t FROM commands WHERE site=? AND kind='announce'", (site,))
    return None if r is None or r["t"] is None else float(r["t"])
