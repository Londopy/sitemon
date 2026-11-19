"""The weekly summary posted to Discord, one message per group of sites."""

from __future__ import annotations

from ..config import HubConfig
from .db import HubDB

WEEK = 7 * 86400.0
HEADLINE = [
    ("v", "battery/supply", "V"),
    ("temp_c", "temperature", "°C"),
]


def contact_percent(db: HubDB, site: str, t0: float, t1: float, bucket: float = 600.0) -> float:
    """Share of 10-minute slots in which the site reported anything."""
    rows = db.q1(
        "SELECT COUNT(DISTINCT CAST(t / ? AS INTEGER)) AS n FROM events "
        "WHERE site=? AND t>=? AND t<?",
        (bucket, site, t0, t1),
    )
    slots = max(int((t1 - t0) / bucket), 1)
    n = int(rows["n"]) if rows else 0
    return min(100.0, 100.0 * n / slots)


def weekly_summary(cfg: HubConfig, db: HubDB, group: str, now: float) -> str:
    t0 = now - WEEK
    lines = [f"**Weekly site summary: {cfg.groups.get(group, group)}**"]
    alerts = db.alerts_since(t0)
    for site in (s for s in cfg.sites if s.group == group):
        parts = [f"contact {contact_percent(db, site.id, t0, now):.0f}%"]
        for metric in db.metrics(site.id):
            source, _, key = metric.partition(".")
            for hk, label, unit in HEADLINE:
                if key != hk:
                    continue
                r = db.q1(
                    "SELECT MIN(value) AS lo, MAX(value) AS hi, AVG(value) AS av FROM points "
                    "WHERE site=? AND metric=? AND t>=?",
                    (site.id, metric, t0),
                )
                if r is not None and r["av"] is not None:
                    parts.append(
                        f"{source} {label} {r['lo']:.1f}-{r['hi']:.1f} {unit} (avg {r['av']:.1f})"
                    )
        n_alerts = sum(1 for a in alerts if a.site == site.id)
        open_now = db.open_alerts(site.id)
        parts.append(f"{n_alerts} alert{'s' if n_alerts != 1 else ''} this week")
        if open_now:
            parts.append("OPEN: " + "; ".join(a.message for a in open_now))
        lines.append(f"- **{site.name}**: " + ", ".join(parts))
    if len(lines) == 1:
        lines.append("No sites configured.")
    return "\n".join(lines)
