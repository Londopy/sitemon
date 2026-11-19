"""The hub service: upload endpoint, dashboard API and the background loops."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import math
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from importlib import resources
from typing import Any

import aiohttp
from aiohttp import web

from .. import __version__
from .._events import PublicKey
from .._systemd import notify, watchdog_interval
from ..config import ChannelConfig, ConfigError, HubConfig, RuleConfig, SiteConfig
from . import alerts, channels, rules
from .db import HubDB
from .ingest import IngestError, IngestResult, ingest
from .summary import weekly_summary

log = logging.getLogger("sitemon.hub")

GIVE_UP_AFTER = 86400.0
SPARK_KEYS = ("v", "a", "temp_c", "rh", "link_count")


class Hub:
    def __init__(self, cfg: HubConfig, db: HubDB | None = None) -> None:
        self.cfg = cfg
        self.db = db or HubDB(cfg.db_path)
        self.sites: dict[str, SiteConfig] = {s.id: s for s in cfg.sites}
        self.keys = load_keys(cfg)
        # Silence counts from the later of these, so neither a hub outage nor
        # a newly added site looks like a dead site.
        self.process_started = time.time()
        self.first_configured: dict[str, float] = {}
        for sid in self.sites:
            v = self.db.get_meta(f"configured:{sid}")
            if v is None:
                v = str(self.process_started)
                self.db.set_meta(f"configured:{sid}", v)
            self.first_configured[sid] = float(v)
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        # Unauthenticated, so this only annotates a real silence.
        self.bad_uploads: dict[str, str] = {}

    def evaluate(self, site_id: str, now: float | None = None) -> list[alerts.Notice]:
        now = time.time() if now is None else now
        site = self.sites[site_id]
        watch_from = max(self.first_configured[site_id], self.process_started)
        findings = rules.evaluate_site(
            self.cfg, self.db, site, now, watch_from, self.bad_uploads.get(site_id)
        )
        return alerts.apply(self.db, self.cfg, site, findings, now)

    def evaluate_all(self, now: float | None = None) -> list[alerts.Notice]:
        out: list[alerts.Notice] = []
        for sid in self.sites:
            out += self.evaluate(sid, now)
        return out

    def after_ingest(self, res: IngestResult, now: float | None = None) -> list[alerts.Notice]:
        """Queue the one-off notices an upload can carry, beyond what the rules see."""
        now = time.time() if now is None else now
        site = self.sites[res.site]
        notices: list[alerts.Notice] = []
        if res.counter_reset:
            notices.append(
                alerts.Notice(
                    site,
                    "warn",
                    f"WARN: {site.name}",
                    "the site's event counter started again from a lower number (SD card "
                    "replaced or an old image restored?). Its readings are still being stored.",
                )
            )
        notices += self.transients(res)
        for n in notices:
            alerts.queue(self.db, self.cfg, n, now)
        return notices

    def transients(self, res: IngestResult) -> list[alerts.Notice]:
        """State rules that went bad and recovered inside one upload, which the
        rules (looking only at the latest value) would never mention."""
        site = self.sites[res.site]
        out = []
        for rule in self.cfg.rules:
            if rule.type != "state" or not rules.applies(rule, site.id):
                continue
            src, _, key = rule.metric.partition(".")
            seen = [
                (t, ev.data[key])
                for ev, t in res.stored
                if ev.source == src and key in ev.data and ev.kind == "state"
            ]
            if not seen:
                continue
            # Start from the value before this upload: the run may have begun in an earlier one.
            before = self.db.point_before(site.id, rule.metric, seen[0][0])
            if before is not None:
                seen.insert(0, (before[0], _as_state(rule, before[1])))
            run = _first_long_run(rule, seen)
            if run is None or self.db.open_alert(site.id, f"rule:{rule.name}") is not None:
                continue
            start, cleared = run
            if cleared is None:
                latest = self.db.latest_value(site.id, rule.metric)
                if latest is not None and rules.state_matches(rule, latest[0]):
                    continue  # still bad: the rule itself alerts once the hold is met
            span = f"at {_hhmm(start)}" + (f", cleared by {_hhmm(cleared)}" if cleared else "")
            out.append(
                alerts.Notice(
                    site,
                    rule.severity,
                    f"{rule.severity.upper()}: {site.name}",
                    f"{rule.name} {span} (already cleared when reported)",
                )
            )
        return out

    def http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": f"sitemon/{__version__}"})
        return self._session

    async def flush_outbox(self, now: float | None = None) -> int:
        """Send what's due. Channels go in parallel so a dead Discord doesn't
        hold up ntfy; within a channel, messages keep their order."""
        now = time.time() if now is None else now
        by_name = {c.name: c for c in self.cfg.channels}
        # A row waits behind any earlier unsent row of its channel that is
        # backing off, so order holds and a dead channel's backlog can't fill
        # the batch ahead of healthy channels.
        rows = self.db.q(
            "SELECT * FROM outbox o WHERE done IS NULL AND next_try <= ? AND NOT EXISTS "
            "(SELECT 1 FROM outbox p WHERE p.channel = o.channel AND p.done IS NULL "
            "AND p.id < o.id AND p.next_try > ?) ORDER BY id LIMIT 200",
            (now, now),
        )
        queues: dict[str, list[Any]] = {}
        for r in rows:
            if r["channel"] not in by_name:
                self.db.x(
                    "UPDATE outbox SET done=?, last_error='channel removed' WHERE id=?",
                    (now, r["id"]),
                )
                continue
            queues.setdefault(r["channel"], []).append(r)
        results = await asyncio.gather(
            *(self._send_rows(by_name[name], q, now) for name, q in queues.items()),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, BaseException):
                raise res
        return sum(res for res in results if isinstance(res, int))

    async def _send_rows(self, ch: ChannelConfig, rows: list[Any], now: float) -> int:
        sent = 0
        for r in rows:
            body = r["body"]
            age = now - r["created"]
            if age > 600:
                body = f"{body}\n(delayed {int(age // 60)} min: the hub could not send it sooner)"
            try:
                await channels.send(self.http(), ch, r["title"], body, r["severity"])
            except channels.ChannelError as e:
                tries = r["tries"] + 1
                if e.permanent or age > GIVE_UP_AFTER:
                    # Retrying can't help, and it would hold back the rest of the channel.
                    self.db.x(
                        "UPDATE outbox SET tries=?, done=?, last_error=? WHERE id=?",
                        (tries, now, f"gave up: {e}", r["id"]),
                    )
                    log.error("gave up sending via %s: %s", ch.name, e)
                    continue
                # Capped low: the rest of the channel waits on this retry.
                delay = min(60 * 2 ** (tries - 1), 300)
                self.db.x(
                    "UPDATE outbox SET tries=?, next_try=?, last_error=? WHERE id=?",
                    (tries, now + delay, str(e), r["id"]),
                )
                log.warning("send via %s failed (try %d): %s", ch.name, tries, e)
                return sent
            self.db.x("UPDATE outbox SET done=? WHERE id=?", (now, r["id"]))
            sent += 1
        return sent

    def maybe_queue_summary(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.cfg.summary_enabled:
            return False
        local = datetime.fromtimestamp(now)
        if local.weekday() != self.cfg.summary_weekday or local.hour != self.cfg.summary_hour:
            return False
        last = float(self.db.get_meta("summary_sent") or 0)
        if now - last < 6 * 86400:
            return False
        self.db.set_meta("summary_sent", str(now))
        for group in self.cfg.groups:
            text = weekly_summary(self.cfg, self.db, group, now)
            for ch in self.cfg.channels:
                # A summary isn't an alert, so the channel's min_severity doesn't apply.
                if ch.type == "discord" and alerts.wants(ch, "critical", group):
                    self.db.x(
                        "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
                        "VALUES (?, ?, ?, 'info', 'Weekly summary', ?)",
                        (ch.name, now, now, text),
                    )
        return True

    async def _every(self, interval: float, fn: Callable[[], Any], first_delay: float = 0) -> None:
        if first_delay:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), first_delay)
        while not self._stop.is_set():
            try:
                r = fn()
                if isinstance(r, Awaitable):
                    await r
            except sqlite3.Error as e:  # disk full or failing: retry next round
                log.error("database error: %s", e)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    def _prune(self) -> None:
        self.db.prune(time.time() - self.cfg.retention)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        host, _, port = self.cfg.listen.rpartition(":")
        await web.TCPSite(runner, host or "127.0.0.1", int(port)).start()
        loops = [
            self._every(30, self.evaluate_all),
            self._every(10, self.flush_outbox),
            self._every(300, self.maybe_queue_summary),
            # Delayed and off the event loop: pruning an old database can take
            # long enough to trip the systemd watchdog.
            self._every(86400, lambda: asyncio.to_thread(self._prune), first_delay=3600),
        ]
        if (wd := watchdog_interval()) is not None:
            loops.append(self._every(wd, lambda: notify("WATCHDOG=1")))
        tasks = [asyncio.create_task(c) for c in loops]
        notify("READY=1")
        log.info("hub listening on %s with %d sites", self.cfg.listen, len(self.sites))
        try:
            # An unexpected error in any loop is a bug: stop the hub and let
            # systemd restart it rather than limp on with a dead loop.
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runner.cleanup()
            if self._session is not None:
                await self._session.close()

    def make_app(self) -> web.Application:
        # A site back from a long outage sends 500-event batches, far below this.
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_post("/api/ingest", self.h_ingest)
        app.router.add_get("/api/groups", self.h_groups)
        app.router.add_get("/api/group/{group}", self.h_group)
        app.router.add_get("/api/site/{site}", self.h_site)
        app.router.add_get("/api/series", self.h_series)
        app.router.add_get("/api/csv", self.h_csv)
        app.router.add_get("/healthz", self.h_health)
        app.router.add_get("/", self.h_page)
        app.router.add_get("/group/{group}", self.h_page)
        app.router.add_get("/site/{site}", self.h_page)
        app.router.add_get("/static/{name}", self.h_static)
        return app

    def _after_upload(self, res: IngestResult) -> None:
        self.after_ingest(res)
        self.evaluate(res.site)

    async def h_ingest(self, req: web.Request) -> web.Response:
        try:
            env = await req.json()
        except ValueError:
            return web.json_response({"error": "not JSON"}, status=400)
        try:
            res = ingest(self.db, self.keys, env)
        except IngestError as e:
            site = env.get("site") if isinstance(env, dict) else None
            if e.status == 403 and isinstance(site, str) and site in self.sites:
                self.bad_uploads[site] = str(e)
            log.warning("rejected upload: %s", e)
            return web.json_response({"error": str(e)}, status=e.status)
        self.bad_uploads.pop(res.site, None)
        for reason in res.quarantined:
            log.warning("set aside an event from %s: %s", res.site, reason)
        # The upload is committed, so acknowledge it before running the rules:
        # a rule failing here must not make the site resend the batch for ever.
        asyncio.get_running_loop().call_soon(self._after_upload, res)
        return web.json_response(
            {
                "acked_through": res.acked_through,
                "stored": len(res.stored),
                "quarantined": len(res.quarantined),
            }
        )

    async def h_health(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": __version__})

    async def h_groups(self, _: web.Request) -> web.Response:
        return web.json_response(
            [
                {"slug": k, "name": v, "sites": sum(1 for s in self.cfg.sites if s.group == k)}
                for k, v in self.cfg.groups.items()
            ]
        )

    def site_summary(self, site: SiteConfig, now: float, spark_hours: float = 24) -> dict[str, Any]:
        latest = self.db.latest(site.id)
        last = self.db.last_contact(site.id)
        open_alerts = self.db.open_alerts(site.id)
        worst = max((alerts.RANK[a.severity] for a in open_alerts), default=0)
        silent = any(a.rule == "silence" for a in open_alerts)
        status = "silent" if silent else {0: "ok", 1: "warn", 2: "critical"}[worst]
        spark: dict[str, list[tuple[float, float]]] = {}
        for metric, (value, _t) in latest.items():
            if metric.rpartition(".")[2] in SPARK_KEYS and isinstance(value, (int, float)):
                spark[metric] = self.db.series(site.id, metric, now - spark_hours * 3600, now, 96)
        return {
            "id": site.id,
            "name": site.name,
            "group": site.group,
            "status": status,
            "last_contact_s": None if last is None else round(now - last),
            "latest": {m: {"value": v, "age_s": round(now - t)} for m, (v, t) in latest.items()},
            "alerts": [
                {
                    "rule": a.rule,
                    "severity": a.severity,
                    "message": a.message,
                    "since_s": round(now - a.opened),
                }
                for a in open_alerts
            ],
            "spark": spark,
        }

    async def h_group(self, req: web.Request) -> web.Response:
        group = req.match_info["group"]
        if group not in self.cfg.groups:
            raise web.HTTPNotFound(text="no such group")
        now = time.time()
        return web.json_response(
            {
                "slug": group,
                "name": self.cfg.groups[group],
                "version": __version__,
                "sites": [self.site_summary(s, now) for s in self.cfg.sites if s.group == group],
            }
        )

    async def h_site(self, req: web.Request) -> web.Response:
        site = self.sites.get(req.match_info["site"])
        if site is None:
            raise web.HTTPNotFound(text="no such site")
        now = time.time()
        out = self.site_summary(site, now, spark_hours=24 * 7)
        out["metrics"] = self.db.metrics(site.id)
        out["group_name"] = self.cfg.groups.get(site.group, site.group)
        out["recent_alerts"] = [
            {
                "rule": a.rule,
                "severity": a.severity,
                "message": a.message,
                "opened": a.opened,
                "resolved": a.resolved,
            }
            for a in self.db.alerts_since(now - 30 * 86400)
            if a.site == site.id
        ][-50:]
        return web.json_response(out)

    def _range(self, req: web.Request) -> tuple[str, str, float, float]:
        site = req.query.get("site", "")
        metric = req.query.get("metric", "")
        if site not in self.sites or not metric:
            raise web.HTTPBadRequest(text="site and metric are required")
        # Only metrics the site has reported, which keeps arbitrary text out of
        # the CSV header and the download filename.
        if not self.db.has_history(site, metric):
            raise web.HTTPNotFound(text="no such metric for this site")
        now = time.time()
        try:
            hours = float(req.query.get("hours", "24"))
            t1 = float(req.query["to"]) if "to" in req.query else now
            t0 = float(req.query["from"]) if "from" in req.query else t1 - hours * 3600
        except ValueError as e:
            raise web.HTTPBadRequest(text="from/to/hours must be numbers") from e
        if not (math.isfinite(t0) and math.isfinite(t1)) or t1 < t0:
            raise web.HTTPBadRequest(text="bad time range")
        return site, metric, max(t0, t1 - self.cfg.retention), t1

    async def h_series(self, req: web.Request) -> web.Response:
        site, metric, t0, t1 = self._range(req)
        return web.json_response(
            {"site": site, "metric": metric, "points": self.db.series(site, metric, t0, t1)}
        )

    async def h_csv(self, req: web.Request) -> web.Response:
        site, metric, t0, t1 = self._range(req)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        # Spreadsheets run a cell starting with = + - @ as a formula.
        header = "'" + metric if metric[:1] in "=+-@" else metric
        w.writerow(["time_utc", "unix_time", header])
        for t, v in self.db.raw_points(site, metric, t0, t1):
            iso = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
            w.writerow([iso, f"{t:.3f}", v])
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{site}_{metric}") + ".csv"
        return web.Response(
            text=buf.getvalue(),
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    async def h_page(self, req: web.Request) -> web.Response:
        if req.path == "/" and len(self.cfg.groups) == 1:
            raise web.HTTPFound(f"/group/{next(iter(self.cfg.groups))}")
        return web.Response(text=_static("index.html"), content_type="text/html")

    async def h_static(self, req: web.Request) -> web.Response:
        name = req.match_info["name"]
        types = {"app.js": "application/javascript", "style.css": "text/css"}
        if name not in types:
            raise web.HTTPNotFound()
        return web.Response(
            text=_static(name), content_type=types[name], headers={"Cache-Control": "no-cache"}
        )


def _first_long_run(
    rule: RuleConfig, seen: list[tuple[float, Any]]
) -> tuple[float, float | None] | None:
    """The first finished run of bad values lasting at least ``rule.hold``, else
    the run still going at the end, as (start, cleared)."""
    start: float | None = None
    for t, v in seen:
        bad = rules.state_matches(rule, v)
        if bad and start is None:
            start = t
        elif not bad and start is not None:
            if t - start >= rule.hold:
                return start, t
            start = None
    return None if start is None else (start, None)


def _as_state(rule: RuleConfig, value: float) -> Any:
    """A stored point back as the value a state rule compares (points hold
    true/false as 1/0)."""
    return bool(value) if isinstance(rule.bad, bool) else value


def _hhmm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def load_keys(cfg: HubConfig) -> dict[str, list[PublicKey]]:
    out: dict[str, list[PublicKey]] = {}
    for s in cfg.sites:
        try:
            out[s.id] = [PublicKey.from_b64(k) for k in s.keys]
        except ValueError as e:
            raise ConfigError(
                f"site {s.id!r}: key is not a public key from `sitemon keygen` ({e})"
            ) from e
    return out


def _static(name: str) -> str:
    return resources.files("sitemon.hub").joinpath("static", name).read_text(encoding="utf-8")
