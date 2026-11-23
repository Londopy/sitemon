from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp.test_utils import TestServer
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Sparkline, Static

from sitemon import SigningKey
from sitemon.agent.runtime import Agent
from sitemon.config import OperatorConfig
from sitemon.hub import commands
from sitemon.hub.db import HubDB
from sitemon.hub.server import Hub
from sitemon.tui import AgentApp, ConfirmCommand, HubApp, SiteScreen, SitesScreen, theme_for

from .conftest import make_hub_config
from .test_agent import agent_cfg, sensors
from .test_hub import Site, feed_at


async def until(pilot: Pilot[None], cond: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out waiting for the TUI"
        await pilot.pause(0.05)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def cells(table: DataTable[object], row: int) -> list[str]:
    return [str(c) for c in table.get_row_at(row)]


async def serve(hub: Hub) -> tuple[TestServer, str]:
    server = TestServer(hub.make_app())
    await server.start_server()
    return server, str(server.make_url("")).rstrip("/")


def two_sites(op: SigningKey | None = None) -> tuple[Hub, Site, Site]:
    s1, s2 = Site("s1"), Site("s2")
    ops = [OperatorConfig("me", op.public_key().to_b64(), ["*"])] if op else []
    hub = Hub(make_hub_config({"s1": s1.key, "s2": s2.key}, operators=ops), db=HubDB(":memory:"))
    now = time.time()
    feed_at(hub, s1, "batt", {"v": 13.2}, now - 120)
    feed_at(hub, s2, "batt", {"v": 11.6}, now - 120)
    feed_at(hub, s2, "batt", {"v": 11.5}, now - 60)
    hub.evaluate("s1", now)
    hub.evaluate("s2", now)
    return hub, s1, s2


async def test_sites_worst_first_then_a_site_page() -> None:
    hub, *_ = two_sites()
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 2)
            assert cells(table, 0)[:2] == ["CRITICAL", "S2"]
            assert "Battery low" in cells(table, 0)[-1] and cells(table, 0)[2] == "11.50 V"
            assert cells(table, 1)[:2] == ["OK", "S1"]

            await pilot.press("enter")
            assert isinstance(app.screen, SiteScreen) and app.screen.site == "s2"
            latest = app.screen.query_one("#latest", DataTable)
            await until(pilot, lambda: latest.row_count > 0)
            assert ["batt.v", "11.50 V"] == cells(latest, 0)[:2]
            assert app.screen.query(Sparkline)
            status = str(app.screen.query_one("#status", Static).render())
            assert status.startswith("CRITICAL") and "Battery low" in status
            assert app.screen.query_one("#relays-none", Static).display
            assert not app.screen.query_one("#relays", DataTable).display
            assert app.screen.query_one("#commands-none", Static).display
            assert not app.screen.query_one("#latest-none", Static).display
            assert not app.screen.check_action("relay", ())

            await pilot.press("escape")
            assert isinstance(app.screen, SitesScreen)
    finally:
        await server.close()


async def test_unreachable_hub_says_so() -> None:
    app = HubApp(f"http://127.0.0.1:{free_port()}")
    async with app.run_test() as pilot:
        problem = app.screen.query_one("#problem", Static)
        await until(pilot, lambda: "can't read" in str(problem.render()))
        assert "Trying again every 15 s" in str(problem.render())
        assert str(app.screen.query_one("#empty", Static).render()) == ""


async def test_relay_command_is_confirmed_signed_and_queued() -> None:
    op = SigningKey.generate()
    hub, s1, _ = two_sites(op)
    feed_at(hub, s1, "relay", {"radio": False}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url, key=op)
        async with app.run_test(size=(120, 50)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 2)
            await pilot.press("down", "enter")
            relays = app.screen.query_one("#relays", DataTable)
            await until(pilot, lambda: relays.row_count == 1)
            assert cells(relays, 0) == ["radio", "off"]

            await pilot.press("p")
            assert isinstance(app.screen, ConfirmCommand)
            await pilot.press("escape")
            assert commands.recent(hub.db, "s1") == []  # cancelled: nothing sent

            await pilot.press("p")
            app.screen.query_one("#seconds", Input).value = "ten"
            await pilot.click("#send")
            assert "isn't a number of seconds" in str(
                app.screen.query_one("#result", Static).render()
            )
            assert commands.recent(hub.db, "s1") == []

            seconds = app.screen.query_one("#seconds", Input)
            seconds.value = "5"
            seconds.focus()
            await pilot.press("enter")
            await until(pilot, lambda: bool(commands.recent(hub.db, "s1")))
            sent = commands.recent(hub.db, "s1")[0]
            assert sent["args"] == {"relay": "radio", "action": "pulse", "seconds": 5.0}
            assert sent["status"] == "queued"
            assert "Sent." in str(app.screen.query_one("#result", Static).render())
    finally:
        await server.close()


async def test_relays_need_a_key_to_command() -> None:
    hub, s1, _ = two_sites()
    feed_at(hub, s1, "relay", {"radio": False}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 50)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 2)
            await pilot.press("down", "enter")
            relays = app.screen.query_one("#relays", DataTable)
            await until(pilot, lambda: relays.row_count == 1)
            assert "--key" in str(app.screen.query_one("#relay-note", Static).render())
            assert not app.screen.check_action("relay", ())
            await pilot.press("p")
            assert isinstance(app.screen, SiteScreen)
    finally:
        await server.close()


async def test_agent_view_shows_the_agents_own_health(tmp_path: Path) -> None:
    port = free_port()
    ag = Agent(
        agent_cfg(tmp_path, ["http://127.0.0.1:9"], local_ui=f"127.0.0.1:{port}"),
        SigningKey.generate(),
        sensors(),
    )
    await ag.sample_once()
    runner = await ag._start_local_ui()
    try:
        app = AgentApp(f"http://127.0.0.1:{port}")
        async with app.run_test(size=(120, 50)) as pilot:
            readings = app.screen.query_one("#readings", DataTable)
            await until(pilot, lambda: readings.row_count > 0)
            assert "batt.v" in [cells(readings, i)[0] for i in range(readings.row_count)]
            hubs = app.screen.query_one("#hubs", DataTable)
            assert cells(hubs, 0)[0] == "http://127.0.0.1:9"
            assert int(cells(hubs, 0)[1]) > 0  # nothing has reached that hub
            assert str(app.screen.query_one("#faults", Static).render()) == "None."
            assert "queue" in str(app.screen.query_one("#status", Static).render())
    finally:
        assert runner is not None
        await runner.cleanup()
        ag.log.close()


def test_theme_follows_the_terminal_background() -> None:
    assert theme_for({"COLORFGBG": "0;15"}) == "ansi-light"
    assert theme_for({"COLORFGBG": "15;0"}) == "ansi-dark"
    assert theme_for({}) == "ansi-dark"
