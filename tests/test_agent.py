from __future__ import annotations

import asyncio
import sqlite3
import math
import os
import socket
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from aiohttp.typedefs import Handler

from sitemon import SigningKey
from sitemon._events import Event
from sitemon.agent.runtime import FAULT_AFTER, Agent, StateTracker, Debouncer
from sitemon.agent.sensors import sim
from sitemon.agent.sensors.factory import Deferred, build_sensors
from sitemon.config import AgentConfig, AmiConfig, ConfigError, SensorConfig
from sitemon.hub.db import HubDB
from sitemon.hub.server import Hub

from .conftest import FakeAmi, make_hub_config


def agent_cfg(tmp: Path, hubs: list[str], **kw: Any) -> AgentConfig:
    base: dict[str, Any] = dict(
        site="s1",
        key_path="",
        data_dir=str(tmp),
        hubs=hubs,
        sample_interval=0.05,
        heartbeat_interval=0.1,
        flush_interval=0.05,
        sync_interval=0.05,
        local_ui="",
        switch_debounce=0,
    )
    base.update(kw)
    return AgentConfig(**base)


def sensors() -> list[Any]:
    return [sim.battery(), sim.environment(), sim.switch("door", "open")]


def recorded(ag: Agent) -> list[Event]:
    """Everything an agent with no hubs has logged (it never deletes any)."""
    ag.log.flush()
    return ag.log.unsent("nobody", 10_000)


class Link:
    """The path to a hub, which a test can cut. While it's down every upload
    fails as over a dead link, but the hub keeps its URL (and no refused
    connect costs 2 s on Windows)."""

    def __init__(self, app: web.Application) -> None:
        self.up = True
        #: Seqs of every upload that got through, in the order they arrived.
        self.uploaded: list[int] = []

        @web.middleware
        async def gate(request: web.Request, handler: Handler) -> web.StreamResponse:
            if not self.up:
                raise web.HTTPBadGateway(text="link down")
            if request.path == "/api/ingest":
                self.uploaded += [e["seq"] for e in (await request.json())["events"]]
            return await handler(request)

        app.middlewares.append(gate)


async def start_hub(key: SigningKey) -> tuple[Hub, TestServer, Link, str]:
    hub = Hub(make_hub_config({"s1": key}), db=HubDB(":memory:"))
    app = hub.make_app()
    link = Link(app)
    srv = TestServer(app)
    await srv.start_server()
    return hub, srv, link, str(srv.make_url("")).rstrip("/")


def hub_seqs(hub: Hub) -> list[int]:
    return [r["seq"] for r in hub.db.q("SELECT seq FROM events WHERE site='s1' ORDER BY seq")]


async def test_sample_records_readings_and_states(tmp_path: Path, key: SigningKey) -> None:
    door = sim.switch("door", "open")
    ag = Agent(agent_cfg(tmp_path, []), key, [sim.battery(), door])
    await ag.sample_once()
    await ag.sample_once()
    evs = [e for e in recorded(ag) if e.kind != "fault"]
    # Two battery readings, but the unchanged door only once.
    assert [(e.kind, e.source) for e in evs] == [
        ("reading", "batt"),
        ("state", "door"),
        ("reading", "batt"),
    ]
    door.overrides["open"] = True
    await ag.fast_once()
    assert recorded(ag)[-1].data == {"open": True}


async def test_sensor_fault_and_recovery(tmp_path: Path, key: SigningKey) -> None:
    env = sim.environment()
    ag = Agent(agent_cfg(tmp_path, []), key, [env])
    env.fail_reads = FAULT_AFTER
    for _ in range(FAULT_AFTER):
        await ag.sample_once()
    assert "env" in ag.faulted
    await ag.sample_once()
    faults = [e.data for e in recorded(ag) if e.kind == "fault"]
    assert faults[0]["ok"] is False and faults[1] == {"ok": True}


async def test_single_glitch_is_not_a_fault(tmp_path: Path, key: SigningKey) -> None:
    env = sim.environment()
    ag = Agent(agent_cfg(tmp_path, []), key, [env])
    env.fail_reads = FAULT_AFTER - 1
    for _ in range(FAULT_AFTER):
        await ag.sample_once()
    faults = [e.data for e in recorded(ag) if e.kind == "fault"]
    # No failure reported, only the one "fine" every sensor sends after start.
    assert not ag.faulted and faults == [{"ok": True}]


async def test_first_good_read_after_start_says_ok_once(tmp_path: Path, key: SigningKey) -> None:
    # A restart forgets which sensors were faulted, and the hub only clears a
    # fault alert on an ok event, so each sensor reports ok once it reads fine.
    env = sim.environment()
    env.fail_reads = 1
    ag = Agent(agent_cfg(tmp_path, []), key, [sim.battery(), env])
    for _ in range(3):
        await ag.sample_once()
    faults = [(e.source, e.data) for e in recorded(ag) if e.kind == "fault"]
    assert faults == [("batt", {"ok": True}), ("env", {"ok": True})]


async def test_non_finite_values_are_failed_reads(tmp_path: Path, key: SigningKey) -> None:
    batt = sim.battery()
    batt.overrides["v"] = math.nan
    ag = Agent(agent_cfg(tmp_path, []), key, [batt])
    for _ in range(FAULT_AFTER):
        await ag.sample_once()
    assert "v=nan" in ag.faulted["batt"]
    batt.overrides["v"] = math.inf
    await ag.sample_once()
    assert ag.failures["batt"] == FAULT_AFTER + 1
    batt.overrides["v"] = 13.1
    await ag.sample_once()
    evs = recorded(ag)
    # Nothing the hub couldn't store got logged as a reading.
    assert [e.data["v"] for e in evs if e.kind == "reading"] == [13.1]
    assert [e.data["ok"] for e in evs if e.kind == "fault"] == [False, True]


async def test_ami_polling(tmp_path: Path, key: SigningKey, fake_ami: FakeAmi) -> None:
    cfg = agent_cfg(
        tmp_path,
        [],
        ami=AmiConfig(
            "127.0.0.1", fake_ami.port, "sitemon", "s3cret", 1999, 1.0, "rpt show registrations"
        ),
    )
    ag = Agent(cfg, key, [])
    await ag.ami_once()
    await ag.ami_once()  # unchanged (uptime ignored): no second event
    fake_ami.answering = False  # Asterisk goes down
    fake_ami.drop_sessions()
    await ag.ami_once()
    assert ag.ami is not None
    await ag.ami.close()
    data = [e.data for e in recorded(ag)]
    assert len(data) == 2
    assert data[0]["connected"] is True and data[0]["link_count"] == 2
    assert data[0]["registered"] is True and 98765 <= data[0]["uptime_s"] <= 98767
    assert data[1]["connected"] is False


async def test_ami_session_dropped_between_polls(
    tmp_path: Path, key: SigningKey, fake_ami: FakeAmi
) -> None:
    # Registration check off (the default): it needs write = command.
    cfg = agent_cfg(
        tmp_path, [], ami=AmiConfig("127.0.0.1", fake_ami.port, "sitemon", "s3cret", 1999, 1.0, "")
    )
    ag = Agent(cfg, key, [])
    ag.states = StateTracker(refresh=0)  # record every poll
    await ag.ami_once()
    fake_ami.drop_sessions()  # Asterisk restarted between polls
    await asyncio.sleep(0.05)
    await ag.ami_once()
    assert ag.ami is not None
    await ag.ami.close()
    data = [e.data for e in recorded(ag)]
    assert [d["connected"] for d in data] == [True, True]
    assert [a["Action"] for a in fake_ami.actions].count("Login") == 2  # one reconnect
    assert all("registered" not in d for d in data)
    assert all(a["Action"] != "Command" for a in fake_ami.actions)


async def test_status_page_port_in_use_does_not_stop_the_agent(
    tmp_path: Path, key: SigningKey, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        ag = Agent(agent_cfg(tmp_path, [], local_ui=f"127.0.0.1:{port}"), key, [sim.battery()])
        task = asyncio.create_task(ag.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "batt" in ag.latest:
                break
        assert not task.done()
        ag.stop()
        await asyncio.wait_for(task, 10)
    assert "batt" in ag.latest  # it kept monitoring, just without the page
    assert "status page not started" in caplog.text


def test_state_tracker_refreshes() -> None:
    t = [0.0]
    st = StateTracker(refresh=100, clock=lambda: t[0])
    assert st.due("door", {"open": False})
    assert st.due("door", {"open": False})  # still due until it's been sent
    st.sent("door", {"open": False})
    assert not st.due("door", {"open": False, "uptime_s": 5})
    t[0] = 101
    assert st.due("door", {"open": False})


async def test_state_is_resent_when_writing_it_failed(
    tmp_path: Path, key: SigningKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    door = sim.switch("door", "open", active=True)
    ag = Agent(agent_cfg(tmp_path, []), key, [door])
    ag.announced.add("door")  # skip the start-up ok event
    real = ag.log.append
    calls = []

    def flaky(kind: str, source: str, data: dict[str, Any]) -> Any:
        calls.append(kind)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(kind, source, data)

    monkeypatch.setattr(ag.log, "append", flaky)
    with pytest.raises(sqlite3.OperationalError):
        await ag.fast_once()
    await ag.fast_once()
    assert [e.data for e in recorded(ag)] == [{"open": True}]


def test_build_sensors_validates_and_defers(tmp_path: Path) -> None:
    out = build_sensors([SensorConfig("ina226", "batt", {"shunt_ohms": 0.002, "bus": 99})])
    assert isinstance(out[0], Deferred)
    with pytest.raises(ConfigError, match="shunt_ohms"):
        build_sensors([SensorConfig("ina226", "batt", {})])
    with pytest.raises(ConfigError, match="unknown type"):
        build_sensors([SensorConfig("dht22", "x", {})])


def test_missing_hardware_is_a_sensor_fault_not_a_crash() -> None:
    from sitemon.agent.sensors.base import SensorError

    (s,) = build_sensors([SensorConfig("bme280", "env", {"bus": 99})])
    with pytest.raises(SensorError):
        s.read()


async def test_store_and_forward_through_an_outage(tmp_path: Path) -> None:
    key = SigningKey.generate()
    hub, srv, link, url = await start_hub(key)
    ag = Agent(agent_cfg(tmp_path, [url]), key, sensors())

    # Link up: a few samples go straight through.
    for _ in range(3):
        await ag.sample_once()
    assert await ag.sync_once()
    assert ag.log.depth() == 0

    # Link down: readings queue on the site.
    link.up = False
    for _ in range(5):
        await ag.sample_once()
    ag.heartbeat()
    assert not await ag.sync_once()
    queued = ag.log.depth()
    assert queued == 5 * 2 + 1  # battery + environment per sample, plus the heartbeat
    assert "502" in ag.last_sync_error

    # Link back: the backlog arrives in order, nothing lost.
    link.up = True
    assert await ag.sync_once(batch=4)  # several small batches
    assert ag.log.depth() == 0 and ag.last_sync_error == ""
    seqs = hub_seqs(hub)
    # Before the outage: battery + environment per sample, the door state
    # once, and each sensor's start-up ok.
    assert seqs == sorted(set(seqs)) and len(seqs) == 3 * 2 + 1 + 3 + queued
    assert link.uploaded == seqs  # in order, each once
    await srv.close()
    if ag._session:
        await ag._session.close()


async def test_every_hub_gets_every_event(tmp_path: Path) -> None:
    key = SigningKey.generate()
    mesh, mesh_srv, mesh_link, mesh_url = await start_hub(key)
    inet, inet_srv, inet_link, inet_url = await start_hub(key)
    ag = Agent(agent_cfg(tmp_path, [mesh_url, inet_url]), key, [sim.battery()])
    await ag.sample_once()
    assert await ag.sync_once()
    assert hub_seqs(mesh) == hub_seqs(inet) != []
    assert ag.log.depth() == 0

    # The internet path dies. The mesh hub still gets everything; the site
    # keeps what the internet hub hasn't had.
    inet_link.up = False
    for _ in range(3):
        await ag.sample_once()
    assert not await ag.sync_once()
    assert len(hub_seqs(mesh)) == len(hub_seqs(inet)) + 3
    assert ag.log.lag(mesh_url) == 0 and ag.log.lag(inet_url) == ag.log.depth() == 3
    ag.heartbeat()
    ag.log.flush()
    hb = ag.log.unsent(inet_url)[-1].data
    assert {u: h["lag"] for u, h in hb["hubs"].items()} == {mesh_url: 0, inet_url: 3}
    assert all(h["last_ok_age_s"] is not None for h in hb["hubs"].values())
    assert hb["queue"] == 3 and hb["last_sync_age_s"] is not None
    st = ag.status()
    assert "link down" in st["hubs"][inet_url]["error"] and st["hubs"][mesh_url]["error"] == ""
    page = await ag._ui_page(make_mocked_request("GET", "/"))
    assert "link down" in str(page.text)

    # It comes back and catches up, in order, without anything resent.
    inet_link.up = True
    assert await ag.sync_once(batch=2)
    assert hub_seqs(inet) == hub_seqs(mesh)
    assert inet_link.uploaded == sorted(set(inet_link.uploaded)) == hub_seqs(inet)
    assert ag.log.depth() == 0

    # Down again, then taken out of the config: the site stops holding
    # events for it.
    inet_link.up = False
    await ag.sample_once()
    assert not await ag.sync_once()
    assert ag.log.depth() == 1
    ag.log.close()
    restarted = Agent(agent_cfg(tmp_path, [mesh_url]), key, [sim.battery()])
    assert restarted.log.depth() == 0
    restarted.log.close()
    await mesh_srv.close()
    await inet_srv.close()
    if ag._session:
        await ag._session.close()


async def test_ack_beyond_the_batch_deletes_nothing_unsent(tmp_path: Path, key: SigningKey) -> None:
    uploads: list[list[int]] = []

    async def ingest(request: web.Request) -> web.Response:
        uploads.append([e["seq"] for e in (await request.json())["events"]])
        if len(uploads) > 1:
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"acked_through": 10**9})

    app = web.Application()
    app.router.add_post("/api/ingest", ingest)
    async with TestServer(app) as srv:
        url = str(srv.make_url("")).rstrip("/")
        ag = Agent(agent_cfg(tmp_path, [url]), key, [sim.battery()])
        for _ in range(3):
            await ag.sample_once()  # the start-up ok and three readings
        assert not await ag.sync_once(batch=2)
        # Only the batch that was sent counts as delivered.
        assert len(uploads) == 2
        assert [e.seq for e in ag.log.unsent(url)] == uploads[1]
        assert ag.log.lag(url) == ag.log.depth() == 2
        if ag._session:
            await ag._session.close()


@pytest.mark.parametrize(
    "reply",
    [
        {"acked_through": 0},
        {"acked_through": -5},
        {"acked_through": "9"},
        {"stored": 3},
        [9],
    ],
)
async def test_reply_that_acks_nothing_is_no_progress(
    tmp_path: Path, key: SigningKey, reply: Any
) -> None:
    async def ingest(_: web.Request) -> web.Response:
        return web.json_response(reply)

    app = web.Application()
    app.router.add_post("/api/ingest", ingest)
    async with TestServer(app) as srv:
        url = str(srv.make_url("")).rstrip("/")
        ag = Agent(agent_cfg(tmp_path, [url]), key, [sim.battery()])
        await ag.sample_once()
        assert not await ag.sync_once()
        assert ag.log.lag(url) == ag.log.depth() == 2
        assert ag.hubs[url].error and ag.hubs[url].last_ok is None
        if ag._session:
            await ag._session.close()


async def test_bad_key_keeps_events_on_site(tmp_path: Path) -> None:
    good, wrong = SigningKey.generate(), SigningKey.generate()
    hub = Hub(make_hub_config({"s1": good}), db=HubDB(":memory:"))
    async with TestServer(hub.make_app()) as srv:
        url = str(srv.make_url("")).rstrip("/")
        ag = Agent(agent_cfg(tmp_path, [url]), wrong, sensors())
        await ag.sample_once()
        assert not await ag.sync_once()
        assert ag.log.depth() == ag.log.lag(url) > 0
        assert "403" in ag.hubs[url].error and "403" in ag.last_sync_error
        if ag._session:
            await ag._session.close()
    assert "s1" in hub.bad_uploads


@pytest.mark.slow
async def test_agent_and_hub_run_together(tmp_path: Path) -> None:
    key = SigningKey.generate()
    hub = Hub(make_hub_config({"s1": key}), db=HubDB(":memory:"))
    batt = sim.battery(float_v=12.0)  # below the 12.2 V warning level
    async with TestServer(hub.make_app()) as srv:
        cfg = agent_cfg(tmp_path, [str(srv.make_url("")).rstrip("/")], local_ui="127.0.0.1:0")
        ag = Agent(
            cfg, key, [batt, sim.switch("door", "open")], db_path=os.path.join(tmp_path, "e.db")
        )
        cfg.local_ui = ""  # port 0 can't be read back from config; skip the page here
        task = asyncio.create_task(ag.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if hub.db.open_alert("s1", "rule:Battery low"):
                break
        ag.stop()
        await task
    assert hub.db.open_alert("s1", "rule:Battery low") is not None
    assert hub.db.latest_value("s1", "agent.queue") is not None


def test_debouncer_ignores_bounce_and_keeps_a_real_change() -> None:
    t = [0.0]
    d = Debouncer(2.0, clock=lambda: t[0])
    shut, open_ = {"open": False}, {"open": True}
    assert d.value("door", shut) == shut  # the first reading is taken as is
    # the reed switch chatters as the door swings
    for i, v in enumerate([open_, shut, open_, shut]):
        t[0] = 0.2 * (i + 1)
        assert d.value("door", v) == shut
    # then it really is open: reported once it has held for 2 s
    t[0] = 1.0
    assert d.value("door", open_) == shut
    t[0] = 2.9
    assert d.value("door", open_) == shut
    t[0] = 3.0
    assert d.value("door", open_) == open_


async def test_bouncing_door_is_recorded_once(tmp_path: Path, key: SigningKey) -> None:
    door = sim.switch("door", "open")
    ag = Agent(agent_cfg(tmp_path, [], switch_debounce=2.0), key, [door])
    t = [0.0]
    ag.switches = Debouncer(2.0, clock=lambda: t[0])
    await ag.fast_once()  # shut
    for i, v in enumerate([True, False, True, True, True]):
        door.overrides["open"] = v
        t[0] = 0.5 * (i + 1)
        await ag.fast_once()
    t[0] = 4.0
    await ag.fast_once()
    states = [e.data for e in recorded(ag) if e.kind == "state"]
    assert states == [{"open": False}, {"open": True}]


async def test_unexpected_error_stops_the_agent_after_cleanup(
    tmp_path: Path, key: SigningKey
) -> None:
    class Broken:
        name = "batt"
        kind: Literal["reading", "state"] = "reading"
        closed = False

        def read(self) -> dict[str, Any]:
            raise RuntimeError("driver bug")

        def close(self) -> None:
            Broken.closed = True

    ag = Agent(agent_cfg(tmp_path, []), key, [Broken()])
    with pytest.raises(RuntimeError, match="driver bug"):
        await asyncio.wait_for(ag.run(), 5)
    assert Broken.closed
    with pytest.raises(sqlite3.ProgrammingError):  # the event log was closed
        ag.log.depth()
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert left == []  # the other loops were cancelled, not left running


async def test_event_log_error_does_not_stop_a_loop(tmp_path: Path, key: SigningKey) -> None:
    ag = Agent(agent_cfg(tmp_path, []), key, [])
    calls = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        ag.stop()

    await asyncio.wait_for(ag._every(0, flaky), 5)
    assert len(calls) == 2
    ag.log.close()


async def test_sync_loop_survives_an_event_log_error(
    tmp_path: Path, key: SigningKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    ag = Agent(agent_cfg(tmp_path, ["http://127.0.0.1:9"]), key, [])
    calls = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        ag.stop()
        return 0

    monkeypatch.setattr(ag.log, "flush", flaky)
    hub = next(iter(ag.hubs.values()))
    await asyncio.wait_for(ag._sync_loop(hub), 10)
    assert len(calls) == 2
    if ag._session:
        await ag._session.close()


def test_rf_sensor_is_built_as_fast() -> None:
    (rf,) = build_sensors([SensorConfig("rf", "rf", {"bus": 99, "coupling_db": 50})])
    assert isinstance(rf, Deferred) and rf.fast is True
    (sim_rf,) = build_sensors([SensorConfig("sim-rf", "rf", {})])
    assert getattr(sim_rf, "fast", False) is True


async def test_rf_recorded_while_transmitting_once_per_interval(
    tmp_path: Path, key: SigningKey
) -> None:
    t = [0.0]
    rf = sim.rf(duty=0.5, clock=lambda: t[0])  # keyed for the first 30 s of each minute
    ag = Agent(agent_cfg(tmp_path, [], sample_interval=10), key, [rf])
    ag.announced.add("rf")
    ag.clock = lambda: t[0]
    for second in range(60):
        t[0] = float(second)
        await ag.fast_once()
    t[0] = 65.0  # keyed again: a slow loop that read fast sensors would record now
    await ag.sample_once()
    readings = [e for e in recorded(ag) if e.kind == "reading"]
    assert len(readings) == 3  # at 0, 10 and 20 s; unkeyed from 30 s on
    assert all(1.2 < e.data["swr"] < 1.4 for e in readings)
    ag.log.close()
