from __future__ import annotations

import dataclasses
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from aiohttp.test_utils import TestServer
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Sparkline, Static

from sitemon import SigningKey
from sitemon.agent.runtime import Agent
from sitemon.config import OperatorConfig
from sitemon.hub import commands
from sitemon.hub.db import HubDB
from sitemon.hub.server import Hub
from sitemon.tui import (
    AgentApp,
    ConfirmCommand,
    HubApp,
    SiteScreen,
    SitesScreen,
    fill,
    readings,
    theme_for,
)

from .conftest import make_hub_config
from .test_agent import agent_cfg, sensors
from .test_hub import Site, feed, feed_at


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


async def test_a_silent_site_leads_with_no_contact() -> None:
    s1 = Site("s1")
    hub = Hub(
        make_hub_config({"s1": s1.key}, silence_warn=300, silence_critical=900),
        db=HubDB(":memory:"),
    )
    now = time.time()
    feed_at(hub, s1, "batt", {"v": 11.6}, now - 700)
    feed_at(hub, s1, "batt", {"v": 11.5}, now - 640)
    hub.evaluate("s1", now - 640)
    hub.evaluate("s1", now + 400)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 1)
            assert cells(table, 0)[0] == "NO CONTACT"
            assert cells(table, 0)[-1].startswith("no contact")
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


async def open_site(app: HubApp, pilot: Pilot[None], row: int = 0, rows: int = 2) -> None:
    table = app.screen.query_one("#sites", DataTable)
    await until(pilot, lambda: table.row_count == rows)
    await pilot.press(*["down"] * row, "enter")


async def test_every_chart_gets_a_line() -> None:
    hub, _, s2 = two_sites()
    feed_at(hub, s2, "env", {"temp_c": 24.0, "rh": 45.0}, time.time() - 60)
    feed_at(hub, s2, "env", {"temp_c": 25.0, "rh": 46.0}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_site(app, pilot)
            await until(pilot, lambda: len(app.screen.query(Sparkline)) >= 3)
            await pilot.pause(0.2)
            charts = app.screen.query_one("#charts")
            assert charts.size.height == len(app.screen.query(Sparkline))
    finally:
        await server.close()


async def test_text_from_a_site_is_shown_as_text() -> None:
    hub, _, s2 = two_sites()
    fault = "chip said [/x] bad \x1b]0;t\x1b\\"
    feed(hub, s2, "fault", "batt", {"ok": False, "error": fault}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_site(app, pilot)
            latest = app.screen.query_one("#latest", DataTable)
            await until(pilot, lambda: latest.row_count > 1)
            texts = [c for row in range(latest.row_count) for c in cells(latest, row)]
            assert any("chip said [/x] bad" in t for t in texts)
            assert not any("\x1b" in t for t in texts)
    finally:
        await server.close()


async def test_a_rule_name_with_brackets_does_not_kill_the_list() -> None:
    s1 = Site("s1")
    cfg = make_hub_config({"s1": s1.key})
    rules = [dataclasses.replace(cfg.rules[0], name="Battery [/x] low"), cfg.rules[1]]
    hub = Hub(dataclasses.replace(cfg, rules=rules), db=HubDB(":memory:"))
    feed_at(hub, s1, "batt", {"v": 11.5}, time.time() - 60)
    hub.evaluate("s1", time.time())
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 1)
            await pilot.pause(0.3)
            assert app.is_running
            assert "Battery [/x] low" in cells(table, 0)[-1]
    finally:
        await server.close()


async def test_a_second_enter_does_not_queue_a_second_command() -> None:
    op = SigningKey.generate()
    hub, s1, _ = two_sites(op)
    feed_at(hub, s1, "relay", {"radio": False}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url, key=op)
        async with app.run_test(size=(120, 50)) as pilot:
            await open_site(app, pilot, row=1)
            relays = app.screen.query_one("#relays", DataTable)
            await until(pilot, lambda: relays.row_count == 1)
            await pilot.press("p")
            seconds = app.screen.query_one("#seconds", Input)
            seconds.value = "5"
            seconds.focus()
            await pilot.press("enter", "enter")
            await pilot.pause(1.0)
            assert len(commands.recent(hub.db, "s1")) == 1
    finally:
        await server.close()


async def test_a_failed_status_poll_does_not_say_nothing_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sitemon.tui as tui

    op = SigningKey.generate()
    hub, s1, _ = two_sites(op)
    feed_at(hub, s1, "relay", {"radio": False}, time.time() - 30)

    async def proxy_page(req: web.Request) -> web.Response:
        return web.Response(text="<html>challenge</html>", content_type="text/html")

    monkeypatch.setattr(hub, "h_command_status", proxy_page)
    from sitemon.client import send_command as real

    async def quick(session: Any, hub_url: str, cmd: Any, wait: float, poll: float = 3.0) -> Any:
        return await real(session, hub_url, cmd, wait, poll=0.2)

    monkeypatch.setattr(tui, "send_command", quick)
    monkeypatch.setattr(tui.ConfirmCommand, "WAIT", 2.0)
    server, url = await serve(hub)
    try:
        app = HubApp(url, key=op)
        async with app.run_test(size=(120, 50)) as pilot:
            await open_site(app, pilot, row=1)
            relays = app.screen.query_one("#relays", DataTable)
            await until(pilot, lambda: relays.row_count == 1)
            await pilot.press("p", "enter")
            result = app.screen.query_one("#result", Static)
            await until(pilot, lambda: bool(commands.recent(hub.db, "s1")))
            await pilot.pause(3.0)
            assert len(commands.recent(hub.db, "s1")) == 1
            assert "Nothing was sent" not in str(result.render())
    finally:
        await server.close()


def four_sites() -> Hub:
    sites = {sid: Site(sid) for sid in ("s1", "s2", "s3", "s4")}
    hub = Hub(make_hub_config({k: v.key for k, v in sites.items()}), db=HubDB(":memory:"))
    now = time.time()
    feed_at(hub, sites["s1"], "batt", {"v": 13.2}, now - 60)
    feed_at(hub, sites["s2"], "batt", {"v": 11.5}, now - 60)
    feed_at(hub, sites["s3"], "batt", {"v": 12.1}, now - 60)
    feed_at(hub, sites["s4"], "batt", {"v": 13.0}, now - 86400)
    for sid in ("s1", "s2", "s3"):
        hub.evaluate(sid, now)
    hub.evaluate("s4", now + 2000)
    return hub


async def test_every_status_is_in_words_and_worst_comes_first() -> None:
    server, url = await serve(four_sites())
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 4)
            assert [cells(table, i)[:2] for i in range(4)] == [
                ["CRITICAL", "S2"],
                ["NO CONTACT", "S4"],
                ["WARN", "S3"],
                ["OK", "S1"],
            ]
    finally:
        await server.close()


async def test_a_key_without_relays_still_leaves_relay_actions_off() -> None:
    op = SigningKey.generate()
    hub, *_ = two_sites(op)
    server, url = await serve(hub)
    try:
        app = HubApp(url, key=op)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_site(app, pilot)
            await until(pilot, lambda: app.screen.query_one("#relays-none", Static).display)
            assert not app.screen.check_action("relay", ())
    finally:
        await server.close()


async def test_escape_and_cancel_close_the_dialog_and_bad_seconds_are_refused() -> None:
    op = SigningKey.generate()
    hub, s1, _ = two_sites(op)
    feed_at(hub, s1, "relay", {"radio": False}, time.time() - 30)
    server, url = await serve(hub)
    try:
        app = HubApp(url, key=op)
        async with app.run_test(size=(120, 50)) as pilot:
            await open_site(app, pilot, row=1)
            relays = app.screen.query_one("#relays", DataTable)
            await until(pilot, lambda: relays.row_count == 1)
            await pilot.press("p")
            await pilot.press("escape")
            assert isinstance(app.screen, SiteScreen)
            await pilot.press("p")
            await pilot.click("#cancel")
            assert isinstance(app.screen, SiteScreen)
            await pilot.press("p")
            for bad in ("0", "-5", "inf", "nan", "1e999"):
                app.screen.query_one("#seconds", Input).value = bad
                await pilot.click("#send")
                said = str(app.screen.query_one("#result", Static).render())
                assert "isn't a number of seconds" in said, bad
            assert commands.recent(hub.db, "s1") == []
    finally:
        await server.close()


async def test_d_cycles_the_range() -> None:
    hub, *_ = two_sites()
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_site(app, pilot)
            seen = []
            for _ in range(4):
                await pilot.press("d")
                screen = app.screen
                assert isinstance(screen, SiteScreen)
                seen.append(screen.days)
            assert seen == [30, 90, 365, 7]
            label = app.screen.query_one("#range")
            await until(pilot, lambda: "Last 7 days" in str(label.render()))
    finally:
        await server.close()


class Bare(App[None]):
    def compose(self) -> ComposeResult:
        yield DataTable(id="t", cursor_type="row")


async def test_fill_keeps_the_cursor_on_the_same_key() -> None:
    app = Bare()
    async with app.run_test():
        t = app.query_one("#t", DataTable)
        t.add_columns("a")
        fill(t, [("a", ["a"]), ("b", ["b"]), ("c", ["c"])])
        t.move_cursor(row=1)
        fill(t, [("c", ["c"]), ("b", ["b"]), ("a", ["a"])])
        assert t.cursor_row == 1
        fill(t, [("c", ["c"]), ("a", ["a"])])
        assert t.cursor_row == 0


async def test_last_data_stays_while_the_hub_is_gone_and_the_error_clears_when_it_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SitesScreen, "every", 4.0)
    hub, *_ = two_sites()
    port = free_port()
    server = TestServer(hub.make_app(), port=port)
    await server.start_server()
    app = HubApp(f"http://127.0.0.1:{port}")
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            problem = app.screen.query_one("#problem", Static)
            await until(pilot, lambda: table.row_count == 2)
            await server.close()
            await until(pilot, lambda: "Showing what was there" in str(problem.render()), 20)
            assert table.row_count == 2
            server = TestServer(hub.make_app(), port=port)
            await server.start_server()
            await until(pilot, lambda: str(problem.render()) == "", 20)
    finally:
        await server.close()


def test_supply_names_each_source_when_there_are_several() -> None:
    one = {"batt.v": {"value": 12.6}, "env.temp_c": {"value": 20.0}}
    assert readings(one, "v") == "12.60 V"
    two = {"batt.v": {"value": 12.6}, "ac.v": {"value": 13.8}}
    assert readings(two, "v") == "ac 13.80 V  batt 12.60 V"
    assert readings({}, "v") == ""


async def test_a_server_that_is_not_a_hub_says_so() -> None:
    async def groups(_: web.Request) -> web.Response:
        return web.json_response([{"name": "no slug here"}])

    app_ = web.Application()
    app_.router.add_get("/api/groups", groups)
    server = TestServer(app_)
    await server.start_server()
    try:
        app = HubApp(str(server.make_url("")).rstrip("/"))
        async with app.run_test() as pilot:
            problem = app.screen.query_one("#problem", Static)
            await until(pilot, lambda: "not like a sitemon hub" in str(problem.render()))
    finally:
        await server.close()


async def test_long_ranges_refresh_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SiteScreen, "every", 0.2)
    hub, *_ = two_sites()
    loads: list[str] = []
    real = Hub.h_site

    async def counted(self: Hub, req: web.Request) -> web.StreamResponse:
        loads.append(req.query.get("days", "7"))
        return await real(self, req)

    monkeypatch.setattr(Hub, "h_site", counted)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_site(app, pilot)
            await until(pilot, lambda: loads.count("7") >= 2)  # 7 days: refreshed on the timer
            await pilot.press("d")
            await until(pilot, lambda: "30" in loads)
            await pilot.pause(1.0)
            assert loads.count("30") == 1
            await pilot.press("r")
            await until(pilot, lambda: loads.count("30") == 2)
    finally:
        await server.close()


async def test_a_slow_load_still_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SitesScreen, "every", 0.2)
    hub, *_ = two_sites()
    real = Hub.h_groups

    async def slow(self: Hub, req: web.Request) -> web.Response:
        import asyncio

        await asyncio.sleep(0.5)
        return await real(self, req)

    monkeypatch.setattr(Hub, "h_groups", slow)
    server, url = await serve(hub)
    try:
        app = HubApp(url)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.screen.query_one("#sites", DataTable)
            await until(pilot, lambda: table.row_count == 2, timeout=5)
    finally:
        await server.close()


async def test_send_command_rides_out_a_garbled_status_reply() -> None:
    from sitemon._commands import Command
    from sitemon.client import send_command

    replies = iter(["{not json", '{"status": "done", "detail": "radio on for 5 s"}'])

    async def queue(_: web.Request) -> web.Response:
        return web.json_response({}, status=202)

    async def status(_: web.Request) -> web.Response:
        return web.Response(text=next(replies), content_type="application/json")

    fake = web.Application()
    fake.router.add_post("/api/commands", queue)
    fake.router.add_get("/api/commands/{nonce}", status)
    server = TestServer(fake)
    await server.start_server()
    cmd = Command.create("s1", "relay", {"relay": "radio", "action": "pulse"})
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            got = await send_command(
                session, str(server.make_url("")), cmd.signed(SigningKey.generate()), 5, poll=0.05
            )
        assert got == ("done", "radio on for 5 s")
    finally:
        await server.close()
