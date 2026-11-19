"""The rules engine: what is wrong at a site right now, and how badly.

Built in: silence (from the hub's own clock, since a dead site can't report
itself), sensor faults and AMI down. Configured: threshold, rate and state rules.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from dataclasses import dataclass
from typing import Any

from .._duration import human_duration
from ..config import HubConfig, RuleConfig, SiteConfig
from .db import Alert, HubDB


@dataclass
class Finding:
    rule: str  # alert key: "rule:<name>", "silence", "fault:<sensor>", "ami", "quarantine"
    severity: str | None
    message: str


def _fmt(v: float, unit: str) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip()


def applies(rule: RuleConfig, site: str) -> bool:
    return any(fnmatch.fnmatchcase(site, pat) for pat in rule.sites)


def check_threshold(
    rule: RuleConfig, value: float, current: Alert | None
) -> tuple[str | None, str]:
    u = rule.unit
    # Hysteresis only holds a level already crossed: the critical band widens
    # only once the alert is critical, or 11.85 V with a warning open would
    # escalate at a critical level of 11.8.
    h_warn = rule.hysteresis if current is not None else 0.0
    h_crit = rule.hysteresis if current is not None and current.severity == "critical" else 0.0
    sev: str | None = None
    why = ""
    if rule.critical_below is not None and value < rule.critical_below + h_crit:
        sev, why = "critical", f"below {_fmt(rule.critical_below, u)}"
    elif rule.critical_above is not None and value > rule.critical_above - h_crit:
        sev, why = "critical", f"above {_fmt(rule.critical_above, u)}"
    elif rule.below is not None and value < rule.below + h_warn:
        sev, why = "warn", f"below {_fmt(rule.below, u)}"
    elif rule.above is not None and value > rule.above - h_warn:
        sev, why = "warn", f"above {_fmt(rule.above, u)}"
    return (
        sev,
        f"{rule.name}: {_fmt(value, u)} ({why})" if sev else f"{rule.name}: {_fmt(value, u)}",
    )


def check_rate(
    rule: RuleConfig, db: HubDB, site: str, t_now: float
) -> tuple[str | None, str] | None:
    """Mean over the last ``smooth`` against the mean one ``window`` earlier,
    or None while there isn't enough history to say."""
    recent, n1 = db.mean(site, rule.metric, t_now - rule.smooth, t_now)
    old, n0 = db.mean(site, rule.metric, t_now - rule.window - rule.smooth, t_now - rule.window)
    if recent is None or old is None or n1 < 3 or n0 < 3:
        return None
    delta = recent - old
    span = human_duration(rule.window)
    u = rule.unit
    if delta < 0:
        fell = -delta
        msg = f"{rule.name}: fell {_fmt(fell, u)} in {span} ({_fmt(old, u)} -> {_fmt(recent, u)})"
        if rule.critical_drop is not None and fell >= rule.critical_drop:
            return "critical", msg
        if rule.drop is not None and fell >= rule.drop:
            return "warn", msg
    else:
        msg = f"{rule.name}: rose {_fmt(delta, u)} in {span} ({_fmt(old, u)} -> {_fmt(recent, u)})"
        if rule.critical_rise is not None and delta >= rule.critical_rise:
            return "critical", msg
        if rule.rise is not None and delta >= rule.rise:
            return "warn", msg
    return None, msg


def state_matches(rule: RuleConfig, value: Any) -> bool:
    bad = rule.bad
    if isinstance(bad, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(bad, bool) and value == bad
    return bool(value == bad)


def _silence(
    cfg: HubConfig,
    db: HubDB,
    site: SiteConfig,
    now: float,
    watch_from: float,
    upload_error: str | None,
) -> Finding:
    last = db.last_contact(site.id)
    start = last if last is not None else watch_from
    # A hub restart only gives grace to sites not already known to be silent.
    if db.open_alert(site.id, "silence") is None:
        start = max(start, watch_from)
    since = now - start
    sev = (
        "critical"
        if since >= cfg.silence_critical
        else "warn"
        if since >= cfg.silence_warn
        else None
    )
    msg = (
        "has never reported to the hub"
        if last is None
        else (f"no contact for {human_duration(now - last)}")
    )
    if upload_error:
        msg += f" (its uploads are being refused: {upload_error})"
    return Finding("silence", sev, msg)


def _good_sql(rule: RuleConfig, critical: bool = False) -> tuple[str, tuple[Any, ...]] | None:
    """An SQL condition on a stored point meaning "not bad" for ``rule`` (only
    its critical levels if ``critical``), or None when hold doesn't apply."""
    if rule.type == "threshold":
        levels = [(rule.critical_below, "<"), (rule.critical_above, ">")]
        if not critical:
            levels += [(rule.below, "<"), (rule.above, ">")]
        parts = [f"value {op} ?" for level, op in levels if level is not None]
        args = tuple(level for level, _ in levels if level is not None)
        return (f"NOT ({' OR '.join(parts)})", args) if parts else None
    if rule.type == "state" and isinstance(rule.bad, (bool, int, float)):
        return "value != ?", (float(rule.bad),)
    return None


def _too_soon(db: HubDB, site: str, rule: RuleConfig, t: float, critical: bool = False) -> bool:
    """Whether a bad condition hasn't lasted ``rule.hold`` yet, so one transmit
    sag doesn't alert. Measured from the stored readings up to the site's
    newest event, since a state is only re-sent every few minutes."""
    good = _good_sql(rule, critical)
    if rule.hold <= 0 or good is None:
        return False
    start = db.bad_since(site, rule.metric, good[0], good[1], until=t)
    newest = db.newest_event(site)
    if start is None or newest is None:
        return False
    return max(newest, t) - start < rule.hold


def _warn_only(rule: RuleConfig) -> RuleConfig:
    return dataclasses.replace(rule, critical_below=None, critical_above=None)


def evaluate_site(
    cfg: HubConfig,
    db: HubDB,
    site: SiteConfig,
    now: float,
    watch_from: float,
    upload_error: str | None = None,
) -> list[Finding]:
    """Everything wrong at ``site`` now. Silence counts from ``watch_from``,
    the later of the site being added and this hub process starting."""
    out = [_silence(cfg, db, site, now, watch_from, upload_error)]
    keep: set[str] = set()  # open alerts this round has no new verdict on
    latest = db.latest(site.id)

    rej = db.rejected_since(site.id, now - 86400)
    out.append(
        Finding(
            "quarantine",
            "warn" if rej else None,
            f"{len(rej)} event(s) from this site were set aside in the last day: {rej[0]}"
            if rej
            else "no events set aside in the last day",
        )
    )

    for metric, (value, _t) in latest.items():
        if metric.endswith(".fault"):
            sensor = metric[: -len(".fault")]
            out.append(
                Finding(
                    f"fault:{sensor}",
                    "warn" if value else None,
                    f"sensor {sensor} is failing: {value}"
                    if value
                    else f"sensor {sensor} is reading again",
                )
            )

    if "ami.connected" in latest:
        ok = latest["ami.connected"][0] is True
        err = latest.get("ami.error", ("", 0))[0]
        out.append(
            Finding(
                "ami",
                None if ok else "critical",
                "AllStar (Asterisk) is answering"
                if ok
                else f"cannot reach AllStar on the node: {err}",
            )
        )

    for rule in cfg.rules:
        key = f"rule:{rule.name}"
        if not applies(rule, site.id):
            continue
        current = db.open_alert(site.id, key)
        if rule.metric not in latest:
            # The site stopped sending this value; nothing else would clear it.
            if current is not None:
                out.append(Finding(key, None, f"{rule.name}: {rule.metric} is no longer reported"))
            continue
        value, t = latest[rule.metric]
        if rule.type == "threshold":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                keep.add(key)
                continue
            sev, msg = check_threshold(rule, float(value), current)
        elif rule.type == "rate":
            res = check_rate(rule, db, site.id, t)
            if res is None:
                keep.add(key)
                continue
            sev, msg = res
        else:
            bad = state_matches(rule, value)
            sev = rule.severity if bad else None
            msg = f"{rule.name}" if bad else f"{rule.name}: cleared"
        if (
            rule.type == "threshold"
            and sev == "critical"
            and (current is None or current.severity != "critical")
            and _too_soon(db, site.id, rule, t, critical=True)
        ):
            # Critical needs the hold at critical levels too, or one sag escalates.
            sev, msg = check_threshold(_warn_only(rule), float(value), current)
            if sev is None:
                if current is not None:
                    keep.add(key)
                continue
        if sev is not None and current is None and _too_soon(db, site.id, rule, t):
            continue
        if (
            sev == "warn"
            and current is not None
            and rule.escalate_after is not None
            and now - current.opened >= rule.escalate_after
        ):
            sev = "critical"
            msg += f" (for {human_duration(now - current.opened)})"
        out.append(Finding(key, sev, msg))

    # A rule renamed or removed from the config would otherwise leave its alert
    # open for ever.
    produced = {f.rule for f in out} | keep
    for a in db.open_alerts(site.id):
        if a.rule not in produced:
            out.append(Finding(a.rule, None, f"{a.message} (no longer checked)"))
    return out
