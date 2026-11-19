from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sitemon.agent import aredn
from sitemon.agent.ami import (
    ALLOWED_COMMANDS,
    AmiClient,
    AmiError,
    parse_corestatus,
    parse_registered,
    parse_xstat,
)

from .conftest import FakeAmi


async def test_node_state(fake_ami: FakeAmi) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    st = await c.node_state(1999, "rpt show registrations")
    await c.close()
    assert st.links == ["2000", "2001"]
    assert st.keyed_tx is False and st.keyed_rx is False
    assert st.registered is True
    assert st.uptime_s is not None and 98765 <= st.uptime_s <= 98767  # from CoreStatus
    assert st.as_data()["link_count"] == 2


async def test_old_command_format(fake_ami: FakeAmi) -> None:
    fake_ami.old_format = True
    fake_ami.registered = False
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    st = await c.node_state(1999, "rpt show registrations")
    await c.close()
    assert st.registered is False and st.as_data()["registered"] is False


async def test_registration_check_is_opt_in(fake_ami: FakeAmi) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    st = await c.node_state(1999)
    await c.close()
    # Left out, not False, so the hub's "not registered" rule can't fire.
    assert st.registered is None and "registered" not in st.as_data()
    assert all(a["Action"] != "Command" for a in fake_ami.actions)


async def test_uptime_permission_denied_is_not_fatal(fake_ami: FakeAmi) -> None:
    # An AMI user set up for an older sitemon has no write = reporting.
    fake_ami.denied.add("corestatus")
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    st = await c.node_state(1999)
    await c.close()
    assert st.uptime_s is None and "uptime_s" not in st.as_data()
    assert st.as_data()["connected"] is True and st.links == ["2000", "2001"]


async def test_bad_login(fake_ami: FakeAmi) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "wrong")
    with pytest.raises(AmiError, match="login failed"):
        await c.connect()


async def test_only_read_actions_are_sent(fake_ami: FakeAmi) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    await c.node_state(1999, "rpt show registrations")
    await c.close()
    assert {a["Action"].lower() for a in fake_ami.actions} <= {
        "login",
        "rptstatus",
        "corestatus",
        "command",
    }
    cmds = [a["Command"] for a in fake_ami.actions if a["Action"].lower() == "command"]
    assert cmds and set(cmds) <= ALLOWED_COMMANDS


@pytest.mark.parametrize(
    "cmd",
    [
        "core stop now",
        "rpt fun 1999 *32000",
        "rpt show registrations\r\nCommand: core stop now",
        "rpt show registrations; core stop now",
    ],
)
async def test_state_changing_commands_are_never_sent(fake_ami: FakeAmi, cmd: str) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    with pytest.raises(AmiError, match="refusing"):
        await c.command(cmd)
    with pytest.raises(AmiError, match="refusing"):
        await c.action("Command", Command=cmd)
    await c.close()
    assert [a["Action"] for a in fake_ami.actions] == ["Login"]


async def test_only_known_actions_leave_the_client(fake_ami: FakeAmi) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    with pytest.raises(AmiError, match="refusing"):
        await c.command("core stop now")  # refused before "not connected"
    await c.connect()
    with pytest.raises(AmiError, match="refusing"):
        await c.action("Originate", Channel="Local/1@default", Application="Playback")
    with pytest.raises(AmiError, match="refusing"):
        await c.action("RptStatus", COMMAND="XStat", NODE="1999\r\nAction: Command")
    await c.close()
    assert [a["Action"] for a in fake_ami.actions] == ["Login"]


@pytest.mark.parametrize("host", ["10.1.2.3", "node.local.mesh", "0.0.0.0"])
def test_refuses_non_loopback(host: str) -> None:
    with pytest.raises(AmiError, match="loopback"):
        AmiClient(host, 5038, "u", "p")


def test_uptime_from_corestatus() -> None:
    started = datetime(2026, 3, 7, 23, 59, 30).timestamp()
    msg = [
        ("Response", "Success"),
        ("ActionID", "sitemon-2"),
        ("CoreStartupDate", "2026-03-07"),
        ("CoreStartupTime", "23:59:30"),
    ]
    assert parse_corestatus(msg, now=started + 98765) == 98765
    # The clock was stepped back since Asterisk started: never negative.
    assert parse_corestatus(msg, now=started - 60) == 0
    with pytest.raises(AmiError, match="start time"):
        parse_corestatus([("Response", "Success"), ("CoreStartupDate", "2026-03-07")])
    with pytest.raises(AmiError, match="Permission denied"):
        parse_corestatus([("Response", "Error"), ("Message", "Permission denied")])


def test_parsers() -> None:
    tx, rx, links = parse_xstat([("Var", "RPT_TXKEYED=1"), ("LinkedNodes", "T2000, L52769")])
    assert tx and not rx and links == ["2000", "52769"]
    assert parse_registered("1999 Registered") and not parse_registered("1999 Unregistered")


SYSINFO: dict[str, Any] = {
    "node": "N0CALL-PI",
    "node_details": {"firmware_version": "3.25.5.0", "model": "Raspberry Pi 4"},
    "sysinfo": {"uptime": "3 days, 4:05", "loads": [0.12, 0.2, 0.3]},
    "meshrf": {"freq": "5805"},
    "link_info": {"10.1.1.1": {"linkType": "RF"}, "10.1.1.2": {"linkType": "DTD"}},
}


def test_aredn_summary() -> None:
    s = aredn.summarize(SYSINFO)
    assert s == {
        "reachable": True,
        "node": "N0CALL-PI",
        "firmware": "3.25.5.0",
        "model": "Raspberry Pi 4",
        "uptime": "3 days, 4:05",
        "load1": 0.12,
        "link_count": 2,
        "rf_links": 1,
        "rf_freq": "5805",
    }
    assert aredn.summarize({}) == {"reachable": True}


async def test_aredn_fetch() -> None:
    app = web.Application()

    async def handler(_: web.Request) -> web.Response:
        # Old firmware serves JSON as text/plain; fetch must not care.
        import json

        return web.Response(text=json.dumps(SYSINFO), content_type="text/plain")

    app.router.add_get("/a/sysinfo", handler)
    async with TestServer(app) as srv, aiohttp.ClientSession() as s:
        out = await aredn.fetch(s, str(srv.make_url("/a/sysinfo")))
    assert out["rf_links"] == 1


async def test_registration_permission_denied_is_unknown_not_false(fake_ami: FakeAmi) -> None:
    # write = command missing: we can't tell, so say nothing rather than
    # raise a false "not registered" alert.
    fake_ami.denied.add("command")
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    st = await c.node_state(1999, "rpt show registrations")
    await c.close()
    assert st.registered is None and "registered" not in st.as_data()


def test_aredn_odd_shapes_are_ignored() -> None:
    odd = {"meshrf": 5, "sysinfo": "n/a", "node_details": [1], "node": "X"}
    assert aredn.summarize(odd) == {"reachable": True, "node": "X"}
