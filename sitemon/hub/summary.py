"""The weekly summary posted to Discord, one message per group of sites."""

from __future__ import annotations

import statistics

from ..config import HubConfig
from .db import HubDB

WEEK = 7 * 86400.0
TREND_DAYS = 30
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


def low_trend(db: HubDB, site: str, metric: str, now: float) -> float | None:
    """How fast the daily low of ``metric`` is moving, per week, over the last
    30 days. A battery that is wearing out sags further each night long
    before it fails. None with less than a week of whole days."""
    rows = db.q(
        "SELECT CAST(t / 86400 AS INTEGER) AS day, MIN(value) AS lo FROM points "
        "WHERE site=? AND metric=? AND t>=? GROUP BY day ORDER BY day",
        (site, metric, now - TREND_DAYS * 86400.0),
    )
    # The first and last day are part days, and their low misses the night.
    pts = [(float(r["day"]), float(r["lo"])) for r in rows][1:-1]
    if len(pts) < 7:
        return None
    # Median of the pairwise slopes (Theil-Sen), so one bad sample doesn't
    # swing it.
    slopes = sorted(
        (y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1 :]
    )
    return 7 * statistics.median(slopes)


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
                if r is None or r["av"] is None:
                    continue
                parts.append(
                    f"{source} {label} {r['lo']:.1f}-{r['hi']:.1f} {unit} (avg {r['av']:.1f})"
                )
                trend = low_trend(db, site.id, metric, now) if hk == "v" else None
                if trend is not None:
                    parts.append(f"{source} {_trend_words(trend)}")
        n_alerts = sum(1 for a in alerts if a.site == site.id)
        open_now = db.open_alerts(site.id)
        parts.append(f"{n_alerts} alert{'s' if n_alerts != 1 else ''} this week")
        if open_now:
            parts.append("OPEN: " + "; ".join(a.message for a in open_now))
        lines.append(f"- **{site.name}**: " + ", ".join(parts))
    if len(lines) == 1:
        lines.append("No sites configured.")
    return "\n".join(lines)


def _trend_words(per_week: float) -> str:
    if abs(per_week) < 0.01:
        return f"nightly low steady over {TREND_DAYS} days"
    way = "falling" if per_week < 0 else "rising"
    return f"nightly low {way} {abs(per_week):.2f} V a week over {TREND_DAYS} days"
