"""Turning findings into notifications: one open alert per site and rule,
notified when it opens, escalates or resolves (and as a reminder while
critical), queued in an outbox so a hub without internet sends them later."""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass

from .._duration import human_duration
from ..config import ChannelConfig, HubConfig, SiteConfig
from .db import HubDB
from .rules import Finding

log = logging.getLogger("sitemon.hub")

RANK = {"info": 0, "warn": 1, "critical": 2}


@dataclass
class Notice:
    site: SiteConfig
    severity: str  # info | warn | critical
    title: str
    body: str


def wants(ch: ChannelConfig, severity: str, group: str) -> bool:
    if RANK[severity] < RANK[ch.min_severity]:
        return False
    return any(fnmatch.fnmatchcase(group, pat) for pat in ch.groups)


def queue(db: HubDB, cfg: HubConfig, n: Notice, now: float) -> int:
    queued = 0
    for ch in cfg.channels:
        if wants(ch, n.severity, n.site.group):
            db.x(
                "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ch.name, now, now, n.severity, n.title, n.body),
            )
            queued += 1
    return queued


def _link(cfg: HubConfig, site: SiteConfig) -> str:
    return f"\n{cfg.public_url}/site/{site.id}" if cfg.public_url else ""


def apply(
    db: HubDB, cfg: HubConfig, site: SiteConfig, findings: list[Finding], now: float | None = None
) -> list[Notice]:
    now = time.time() if now is None else now
    # One transaction, so an alert is never recorded without its notification.
    with db.tx():
        return _apply(db, cfg, site, findings, now)


def _apply(
    db: HubDB, cfg: HubConfig, site: SiteConfig, findings: list[Finding], now: float
) -> list[Notice]:
    notices: list[Notice] = []
    for f in findings:
        cur = db.open_alert(site.id, f.rule)
        if f.severity is None:
            if cur is not None:
                db.x(
                    "UPDATE alerts SET resolved=?, updated=?, message=? WHERE id=?",
                    (now, now, f.message, cur.id),
                )
                notices.append(
                    Notice(
                        site,
                        cur.notified_severity,
                        f"Resolved: {site.name}",
                        f"{f.message} (after {human_duration(now - cur.opened)}){_link(cfg, site)}",
                    )
                )
            continue
        if cur is None:
            db.x(
                "INSERT INTO alerts (site, rule, severity, opened, updated, message, notified, "
                "notified_severity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (site.id, f.rule, f.severity, now, now, f.message, now, f.severity),
            )
            notices.append(
                Notice(
                    site,
                    f.severity,
                    f"{f.severity.upper()}: {site.name}",
                    f.message + _link(cfg, site),
                )
            )
        elif RANK[f.severity] > RANK[cur.severity]:
            db.x(
                "UPDATE alerts SET severity=?, updated=?, message=?, notified=?, "
                "notified_severity=? WHERE id=?",
                (f.severity, now, f.message, now, f.severity, cur.id),
            )
            notices.append(
                Notice(
                    site,
                    f.severity,
                    f"ESCALATED to {f.severity.upper()}: {site.name}",
                    f.message + _link(cfg, site),
                )
            )
        elif (
            f.severity == "critical"
            and cfg.remind_critical > 0
            and now - cur.notified >= cfg.remind_critical
        ):
            db.x(
                "UPDATE alerts SET updated=?, message=?, notified=? WHERE id=?",
                (now, f.message, now, cur.id),
            )
            notices.append(
                Notice(
                    site,
                    "critical",
                    f"STILL CRITICAL: {site.name}",
                    f"{f.message} (since {human_duration(now - cur.opened)} ago){_link(cfg, site)}",
                )
            )
        else:
            db.x(
                "UPDATE alerts SET severity=?, updated=?, message=? WHERE id=?",
                (f.severity, now, f.message, cur.id),
            )
    for n in notices:
        queue(db, cfg, n, now)
    return notices
