"""The agent's main loop: read everything, log it, ship it to every hub."""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import socket
import logging
import math
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from .. import __version__
from .._commands import Command
from .._events import Event, PublicKey, SigningKey, canonical_json, envelope_body
from .._systemd import notify, watchdog_interval
from ..config import MESH, AgentConfig
from . import aredn
from .ami import AmiClient, AmiError
from .announce import AnnounceError, Announcer
from .relays import KeepAlive, Output, RelayBank, RelayError, gpio_output
from .sensors.base import Sensor, SensorError
from .store import EventLog

log = logging.getLogger("sitemon.agent")

MAX_COMMANDS = 20
# One glitch at a transmitter site is normal; three in a row is a fault.
FAULT_AFTER = 3
# Quiet inputs are re-sent this often so nobody wonders whether they're stale.
STATE_REFRESH = 300.0
# Before this the Pi's clock was never set (no RTC, no NTP yet).
SANE_CLOCK = 1_577_836_800.0  # 2020-01-01
# Change on every poll, so they don't count as a state change.
VOLATILE = {"uptime_s", "uptime", "load1"}


def _seconds(v: Any) -> float | None:
    """A command's optional ``seconds`` as a float; RelayBank checks its range."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RelayError("seconds must be a number")
    try:
        return float(v)
    except OverflowError as e:
        raise RelayError("seconds is out of range") from e


def _is_fast(s: Sensor) -> bool:
    return s.kind == "reading" and getattr(s, "fast", False) is True


def sign_envelope(key: SigningKey, env: dict[str, Any]) -> str:
    return key.sign(canonical_json(envelope_body(env)))


def _age_s(now: float, t: float | None) -> int | None:
    return None if t is None else int(now - t)


def _ago(age_s: int | None) -> str:
    return "never" if age_s is None else f"{age_s} s ago"


class SyncError(Exception):
    """An upload to one hub failed."""


class NoMeshResolver(AbstractResolver):
    """Drops AREDN mesh addresses from DNS answers, so a control hub whose name
    resolves onto the mesh is never reached that way (47 CFR 97.213)."""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self.inner = inner or aiohttp.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        found = await self.inner.resolve(host, port, family)
        kept = [r for r in found if ipaddress.ip_address(r["host"]) not in MESH]
        if not kept:
            raise OSError(f"{host} resolves only to AREDN mesh addresses (10.x)")
        return kept

    async def close(self) -> None:
        await self.inner.close()


def _acked_through(body: Any) -> int:
    # The reply isn't signed: anything but a plain integer is a failed upload.
    ack = body.get("acked_through") if isinstance(body, dict) else None
    if not isinstance(ack, int) or isinstance(ack, bool):
        raise SyncError(f"unexpected reply: {str(body)[:200]}")
    return ack


@dataclass
class HubSync:
    """Upload progress for one hub. ``last_ok`` is monotonic time."""

    url: str
    last_ok: float | None = None
    error: str = ""


class StateTracker:
    """Says when a state is worth recording: it changed, or it's due a refresh."""

    def __init__(self, refresh: float = STATE_REFRESH, clock: Callable[[], float] = time.monotonic):
        self.refresh = refresh
        self.clock = clock
        self._last: dict[str, tuple[dict[str, Any], float]] = {}

    def due(self, source: str, data: dict[str, Any]) -> bool:
        prev = self._last.get(source)
        return (
            prev is None or prev[0] != self._stable(data) or self.clock() - prev[1] >= self.refresh
        )

    def sent(self, source: str, data: dict[str, Any]) -> None:
        self._last[source] = (self._stable(data), self.clock())

    @staticmethod
    def _stable(data: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in data.items() if k not in VOLATILE}


class Debouncer:
    """Holds a switch at its last settled value until a new one has lasted
    ``hold`` seconds, so a bouncing reed switch logs one change, not ten."""

    def __init__(self, hold: float, clock: Callable[[], float] = time.monotonic):
        self.hold = hold
        self.clock = clock
        self._settled: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, tuple[dict[str, Any], float]] = {}

    def value(self, source: str, data: dict[str, Any]) -> dict[str, Any]:
        settled = self._settled.get(source)
        if settled is None or data == settled or self.hold <= 0:
            self._settled[source] = data
            self._pending.pop(source, None)
            return data
        now = self.clock()
        pending = self._pending.get(source)
        if pending is None or pending[0] != data:
            self._pending[source] = (data, now)
        elif now - pending[1] >= self.hold:
            self._settled[source] = data
            del self._pending[source]
            return data
        return settled


class Agent:
    def __init__(
        self,
        cfg: AgentConfig,
        key: SigningKey,
        sensors: list[Sensor],
        *,
        db_path: str | None = None,
        ami: AmiClient | None = None,
        output: Callable[[int, bool], Output] = gpio_output,
    ) -> None:
        self.cfg = cfg
        self.key = key
        self.sensors = sensors
        self.log = EventLog(
            db_path or os.path.join(cfg.data_dir, "events.db"),
            cfg.site,
            key,
            hubs=cfg.hubs,
            max_events=cfg.max_events,
        )
        # Each hub syncs on its own, so a dead internet path never holds up the mesh.
        self.hubs = {url: HubSync(url) for url in self.log.hubs}
        self.ami = ami
        if ami is None and cfg.ami is not None:
            self.ami = AmiClient(
                cfg.ami.host,
                cfg.ami.port,
                cfg.ami.user,
                cfg.ami.secret,
                sound_dir=self._sound_dir() if cfg.announce else None,
            )
        control = cfg.control
        self.control_hubs = set(control.hubs) if control else set()
        self.operator_keys = [PublicKey.from_b64(k) for k in control.operators] if control else []
        self.hub_keys = [PublicKey.from_b64(k) for k in control.hub_keys] if control else []
        self.relays = RelayBank(cfg.relays, output) if cfg.relays else None
        self.keepalive = (
            KeepAlive(control.watchdog_pin, output)
            if control and control.watchdog_pin is not None
            else None
        )
        self.relay_watchdogs = {
            r.name: KeepAlive(r.watchdog_pin, output)
            for r in cfg.relays
            if r.watchdog_pin is not None
        }
        self.announcer = (
            Announcer(cfg.announce, self.ami, cfg.ami.node, self._sound_dir())
            if cfg.announce and self.ami is not None and cfg.ami is not None
            else None
        )
        self._fast_recorded: dict[str, float] = {}
        self.clock: Callable[[], float] = time.monotonic
        self.states = StateTracker()
        self.switches = Debouncer(cfg.switch_debounce)
        self.latest: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, int] = {}
        self.faulted: dict[str, str] = {}
        self.announced: set[str] = set()
        self.started = time.monotonic()
        self.last_sample = time.monotonic()
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._control_session: aiohttp.ClientSession | None = None

    def _sound_dir(self) -> str:
        return os.path.abspath(os.path.join(self.cfg.data_dir, "announce"))

    @property
    def last_sync_ok(self) -> float | None:
        return max((h.last_ok for h in self.hubs.values() if h.last_ok is not None), default=None)

    @property
    def last_sync_error(self) -> str:
        if not self.hubs:
            return "no hubs configured"
        return "; ".join(f"{h.url}: {h.error}" for h in self.hubs.values() if h.error)

    def record(self, kind: str, source: str, data: dict[str, Any]) -> None:
        self.log.append(kind, source, data)
        if kind in ("reading", "state"):
            self.latest[source] = {**data, "_t": time.time()}

    async def read_sensor(self, s: Sensor) -> dict[str, Any] | None:
        try:
            values = await asyncio.to_thread(s.read)
            # The hub can't store NaN or infinity, and a chip returning one is misbehaving.
            bad = [
                f"{k}={v}"
                for k, v in values.items()
                if isinstance(v, float) and not math.isfinite(v)
            ]
            if bad:
                raise SensorError(f"{s.name}: non-finite reading {', '.join(bad)}")
        except SensorError as e:
            n = self.failures.get(s.name, 0) + 1
            self.failures[s.name] = n
            if n >= FAULT_AFTER and s.name not in self.faulted:
                self.record("fault", s.name, {"ok": False, "error": str(e)})
                self.faulted[s.name] = str(e)
                log.warning("sensor %s faulted: %s", s.name, e)
            return None
        self.failures[s.name] = 0
        # The hub clears a fault only on an ok event, and a restart forgets
        # which sensors were faulted, so the first good read after start says so.
        if s.name in self.faulted or s.name not in self.announced:
            self.record("fault", s.name, {"ok": True})
            if self.faulted.pop(s.name, None) is not None:
                log.info("sensor %s recovered", s.name)
            self.announced.add(s.name)
        return dict(values)

    def _send_state(self, source: str, data: dict[str, Any]) -> None:
        # Marked sent only once recorded, so a failed write is retried next poll.
        if self.states.due(source, data):
            self.record("state", source, data)
            self.states.sent(source, data)

    def _record_state(self, s: Sensor, values: dict[str, Any]) -> None:
        values = self.switches.value(s.name, values)
        self._send_state(s.name, values)

    async def sample_once(self) -> None:
        for s in self.sensors:
            if _is_fast(s):
                continue
            values = await self.read_sensor(s)
            if not values:
                continue
            if s.kind == "reading":
                self.record("reading", s.name, values)
            else:
                self._record_state(s, values)
        self.last_sample = time.monotonic()

    async def fast_once(self) -> None:
        """The 1 s sensor loop: switches, and sensors that only have something to
        say briefly (RF while transmitting)."""
        now = self.clock()
        for s in self.sensors:
            if s.kind == "state":
                values = await self.read_sensor(s)
                if values is not None:
                    self._record_state(s, values)
            elif _is_fast(s):
                last = self._fast_recorded.get(s.name)
                if last is not None and now - last < self.cfg.sample_interval:
                    continue
                values = await self.read_sensor(s)
                if values:
                    self.record("reading", s.name, values)
                    self._fast_recorded[s.name] = now

    def relays_once(self) -> None:
        """Relay time limits and the keep-alive pin, in a loop of their own so
        a slow sensor read can never hold a relay on past its limit."""
        if self.relays is not None and self.relays.tick():
            self._send_state("relay", self.relays.states())
        if not self._healthy():
            return
        if self.keepalive is not None:
            self.keepalive.toggle()
        if self.relays is not None:
            # A relay's own watchdog only runs while that relay is meant to be on,
            # so a pin stuck on by a driver bug still drops out.
            for name, wd in self.relay_watchdogs.items():
                if self.relays.relays[name].is_on:
                    wd.toggle()

    def _power_down(self, close: bool = False) -> None:
        """Switch every relay off (and with ``close``, release the pins)."""
        steps = []
        if self.relays is not None:
            steps.append(self.relays.close if close else self.relays.off_all)
        if close and self.keepalive is not None:
            steps.append(self.keepalive.close)
        if close:
            steps += [wd.close for wd in self.relay_watchdogs.values()]
        for step in steps:
            try:
                step()
            # Each step tries every relay; shutdown carries on either way, and
            # the hardware cutoff takes over once the keep-alive stops.
            except Exception as e:
                log.error("could not switch every relay off: %s", e)

    def _healthy(self) -> bool:
        return time.monotonic() - self.last_sample < max(3 * self.cfg.sample_interval, 60)

    async def poll_commands(self, hub: HubSync) -> int:
        """Collect and run commands from a control hub; returns how many arrived."""
        env = self._envelope([])
        url = hub.url.rstrip("/") + "/api/commands/poll"
        try:
            async with self._control_http().post(
                url, json=env, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status != 200:
                    raise SyncError(f"HTTP {r.status} {(await r.text())[:200]}")
                body = await r.json()
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            RecursionError,
            SyncError,
        ) as e:
            log.info("command poll to %s failed: %s", hub.url, e)
            return 0
        raw = body.get("commands") if isinstance(body, dict) else None
        # Bounded, so a misbehaving hub can't flood the log with refusals.
        items = raw[:MAX_COMMANDS] if isinstance(raw, list) else []
        for item in items:
            await self.handle_command(item)
        return len(items)

    async def handle_command(self, raw: Any) -> None:
        try:
            cmd = Command.from_dict(raw)
        except ValueError as e:
            log.warning("ignored a malformed command: %s", e)
            return
        refused = "the agent is stopping" if self._stop.is_set() else self._refuse(cmd)
        if refused is None and not self.log.first_use(cmd.nonce, cmd.expires):
            return  # already handled: the hub resends until it hears back
        ok, detail = (False, refused) if refused else await self._run_command(cmd)
        log.warning("command %s %s: %s", cmd.kind, "done" if ok else "refused", detail)
        self.record(
            "command", "control", {"nonce": cmd.nonce, "kind": cmd.kind, "ok": ok, "detail": detail}
        )

    def _refuse(self, cmd: Command) -> str | None:
        if cmd.site != self.cfg.site:
            return f"for site {cmd.site[:40]!r}, not this one"
        keys = self.operator_keys if cmd.kind == "relay" else self.hub_keys
        if cmd.signer(keys) is None:
            return "not signed by a key this site accepts"
        now = time.time()
        # Expiry means nothing on a clock that was never set.
        if now < SANE_CLOCK:
            return "this site's clock isn't set"
        return cmd.problem(now)

    async def _run_command(self, cmd: Command) -> tuple[bool, str]:
        try:
            if cmd.kind == "relay":
                if self.relays is None:
                    return False, "this site has no relays"
                relay, action = cmd.args.get("relay"), cmd.args.get("action")
                if not isinstance(relay, str) or not isinstance(action, str):
                    return False, "relay and action must be names"
                detail = self.relays.run(relay, action, _seconds(cmd.args.get("seconds")))
                self._send_state("relay", self.relays.states())
                return True, detail
            if self.announcer is None:
                return False, "announcements are off at this site"
            text = cmd.args.get("text")
            if not isinstance(text, str):
                return False, "text must be a string"
            return True, await self.announcer.say(text, cmd.nonce)
        except (RelayError, AnnounceError) as e:
            return False, str(e)

    async def ami_once(self) -> None:
        assert self.ami is not None
        # asyncio only notices Asterisk closed a session when it's next used,
        # so a failed poll on an old session gets one retry on a fresh one.
        tries = 2 if self.ami.connected else 1
        for _ in range(tries):
            try:
                data = await self._ami_poll()
                break
            except (AmiError, OSError, asyncio.TimeoutError) as e:
                await self.ami.close()
                data = {"connected": False, "error": str(e) or type(e).__name__}
        self._send_state("ami", data)

    async def _ami_poll(self) -> dict[str, Any]:
        assert self.ami is not None and self.cfg.ami is not None
        if not self.ami.connected:
            await self.ami.connect()
        st = await self.ami.node_state(self.cfg.ami.node, self.cfg.ami.registration_command)
        return st.as_data()

    async def aredn_once(self) -> None:
        assert self.cfg.aredn is not None
        try:
            data = await aredn.fetch(self._http(), self.cfg.aredn.url)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            data = {"reachable": False, "error": str(e) or type(e).__name__}
        self._send_state("aredn", data)

    def heartbeat(self) -> None:
        now = time.monotonic()
        self.record(
            "heartbeat",
            "agent",
            {
                "version": __version__,
                "uptime_s": int(now - self.started),
                "queue": self.log.depth(),
                "dropped": self.log.dropped,
                "last_sync_age_s": _age_s(now, self.last_sync_ok),
                "hubs": self._hub_info(now),
                "faults": sorted(self.faulted),
            },
        )

    def _hub_info(self, now: float) -> dict[str, dict[str, Any]]:
        return {
            h.url: {"lag": self.log.lag(h.url), "last_ok_age_s": _age_s(now, h.last_ok)}
            for h in self.hubs.values()
        }

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": f"sitemon/{__version__}"})
        return self._session

    def _control_http(self) -> aiohttp.ClientSession:
        """For command polls: never connects to an AREDN mesh address."""
        if self._control_session is None or self._control_session.closed:
            self._control_session = aiohttp.ClientSession(
                headers={"User-Agent": f"sitemon/{__version__}"},
                connector=aiohttp.TCPConnector(resolver=NoMeshResolver()),
            )
        return self._control_session

    async def sync_once(self, batch: int = 500) -> bool:
        """Bring every hub up to date. True when all of them are."""
        self.log.flush()
        # Wait for every hub before raising, so no upload outlives the session.
        done = await asyncio.gather(
            *(self.sync_hub(h, batch) for h in self.hubs.values()), return_exceptions=True
        )
        for r in done:
            if isinstance(r, BaseException):
                raise r
        return all(done)

    async def sync_hub(self, hub: HubSync, batch: int = 500) -> bool:
        """Send ``hub`` what it hasn't confirmed, oldest first. True once it has
        everything, False when it stops taking events."""
        while True:
            events = self.log.unsent(hub.url, batch)
            if not events:
                return True
            try:
                acked = await self._post(hub.url, self._envelope(events))
            except SyncError as e:
                hub.error = str(e)
                log.info("sync to %s failed: %s", hub.url, e)
                return False
            first, last = events[0].seq, events[-1].seq
            # The reply is unsigned: never believe an ack beyond what we sent.
            acked = min(acked, last)
            if acked < first:
                hub.error = f"hub took none of seq {first}-{last} (acked_through {acked})"
                log.info("sync to %s failed: %s", hub.url, hub.error)
                return False
            self.log.ack(hub.url, acked)
            hub.last_ok = time.monotonic()
            hub.error = ""
            if acked < last:
                return False

    def _envelope(self, events: list[Event]) -> dict[str, Any]:
        env: dict[str, Any] = {
            "site": self.cfg.site,
            "boot": self.log.boot,
            "mono_ns": time.monotonic_ns(),
            "wall": round(time.time(), 3),
            "events": [e.to_dict() for e in events],
        }
        env["sig"] = sign_envelope(self.key, env)
        return env

    async def _post(self, hub: str, env: dict[str, Any]) -> int:
        url = hub.rstrip("/") + "/api/ingest"
        try:
            async with self._http().post(
                url, json=env, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status != 200:
                    raise SyncError(f"HTTP {r.status} {(await r.text())[:200]}")
                return _acked_through(await r.json())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RecursionError) as e:
            raise SyncError(str(e) or type(e).__name__) from e

    async def _every(self, interval: float, fn: Callable[[], Any]) -> None:
        while not self._stop.is_set():
            try:
                r = fn()
                if isinstance(r, Awaitable):
                    await r
            except sqlite3.Error as e:
                # A failing card: the log keeps unwritten events buffered, so retry.
                log.error("event log error: %s", e)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    async def _sync_loop(self, hub: HubSync) -> None:
        delay = self.cfg.sync_interval
        while not self._stop.is_set():
            try:
                self.log.flush()
                ok = await self.sync_hub(hub)
            except sqlite3.Error as e:
                log.error("event log error: %s", e)
                ok = False
            # Results go straight back, so whoever sent the command sees them now.
            if ok and hub.url in self.control_hubs and await self.poll_commands(hub):
                self.log.flush()
                await self.sync_hub(hub)
            # Back off up to 5 minutes while this hub's path is down, so a
            # dead link doesn't keep a cellular modem busy.
            delay = self.cfg.sync_interval if ok else min(delay * 2, 300.0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay)

    async def _watchdog_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            # Only ping while sampling is alive, so systemd restarts a wedged agent.
            if time.monotonic() - self.last_sample < max(3 * self.cfg.sample_interval, 60):
                notify("WATCHDOG=1")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    def stop(self) -> None:
        self._stop.set()
        self._power_down()

    def _loops(self) -> list[Coroutine[Any, Any, None]]:
        loops = [
            self._every(self.cfg.sample_interval, self.sample_once),
            self._every(self.cfg.heartbeat_interval, self.heartbeat),
            self._every(self.cfg.flush_interval, self.log.flush),
            *(self._sync_loop(h) for h in self.hubs.values()),
        ]
        if any(s.kind == "state" or _is_fast(s) for s in self.sensors):
            loops.append(self._every(self.cfg.switch_interval, self.fast_once))
        if self.relays is not None or self.keepalive is not None:
            loops.append(self._every(1.0, self.relays_once))
        if self.ami is not None and self.cfg.ami is not None:
            loops.append(self._every(self.cfg.ami.interval, self.ami_once))
        if self.cfg.aredn is not None:
            loops.append(self._every(self.cfg.aredn.interval, self.aredn_once))
        if (wd := watchdog_interval()) is not None:
            loops.append(self._watchdog_loop(wd))
        return loops

    async def run(self) -> None:
        if self.relays is not None:
            self._send_state("relay", self.relays.states())  # all off after a restart
        tasks = [asyncio.create_task(c) for c in self._loops()]
        runner = await self._start_local_ui() if self.cfg.local_ui else None
        notify("READY=1")
        log.info("agent for site %s running (%d sensors)", self.cfg.site, len(self.sensors))
        try:
            # An unexpected error in any loop is a bug: stop and let systemd
            # restart the agent rather than run on with that loop dead.
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Relays before anything that can wait on the network.
            self._power_down(close=True)
            if runner is not None:
                await runner.cleanup()
            if self.ami is not None:
                await self.ami.close()
            with contextlib.suppress(sqlite3.Error):
                await self.sync_once()
            for session in (self._session, self._control_session):
                if session is not None:
                    await session.close()
            for s in self.sensors:
                with contextlib.suppress(SensorError, OSError):
                    s.close()
            self.log.close()

    async def _start_local_ui(self) -> web.AppRunner | None:
        host, _, port = self.cfg.local_ui.rpartition(":")
        app = web.Application()
        app.router.add_get("/", self._ui_page)
        app.router.add_get("/status.json", self._ui_json)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host or "127.0.0.1", int(port)).start()
        except (OSError, ValueError, OverflowError) as e:
            # The page is a convenience; a taken port must not stop the monitoring.
            await runner.cleanup()
            log.warning("local status page not started on %s: %s", self.cfg.local_ui, e)
            return None
        return runner

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "site": self.cfg.site,
            "version": __version__,
            "uptime_s": int(now - self.started),
            "queue": self.log.depth(),
            "dropped": self.log.dropped,
            "last_sync_age_s": _age_s(now, self.last_sync_ok),
            "last_sync_error": self.last_sync_error,
            "hubs": {
                url: {**info, "error": self.hubs[url].error}
                for url, info in self._hub_info(now).items()
            },
            "faults": self.faulted,
            "latest": self.latest,
        }

    async def _ui_json(self, _: web.Request) -> web.Response:
        return web.json_response(self.status())

    async def _ui_page(self, _: web.Request) -> web.Response:
        st = self.status()
        rows = []
        for src, vals in sorted(st["latest"].items()):
            age = int(time.time() - vals.get("_t", time.time()))
            items = ", ".join(
                f"{html.escape(str(k))}: {html.escape(str(v))}"
                for k, v in vals.items()
                if not k.startswith("_")
            )
            rows.append(f"<tr><th>{html.escape(src)}</th><td>{items}</td><td>{age} s ago</td></tr>")
        faults = (
            "".join(
                f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>"
                for k, v in st["faults"].items()
            )
            or "<li>none</li>"
        )
        hubs = (
            "".join(
                f"<tr><th>{html.escape(url)}</th><td>{h['lag']} to send</td>"
                f"<td>last sync {_ago(h['last_ok_age_s'])}</td>"
                f"<td><b>{html.escape(h['error'])}</b></td></tr>"
                for url, h in st["hubs"].items()
            )
            or "<tr><td>no hubs configured</td></tr>"
        )
        body = f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>sitemon: {html.escape(st["site"])}</title>
<style>body{{font:15px system-ui,sans-serif;margin:1rem;max-width:60rem}}
th,td{{text-align:left;padding:.25rem .6rem;border-bottom:1px solid #8884}}</style>
<h1>{html.escape(st["site"])}</h1>
<p>sitemon {html.escape(st["version"])} - up {st["uptime_s"]} s -
queue {st["queue"]} - last sync {_ago(st["last_sync_age_s"])}</p>
<h2>Hubs</h2><table>{hubs}</table>
<h2>Latest</h2><table>{"".join(rows) or "<tr><td>nothing yet</td></tr>"}</table>
<h2>Sensor faults</h2><ul>{faults}</ul>"""
        return web.Response(text=body, content_type="text/html")
