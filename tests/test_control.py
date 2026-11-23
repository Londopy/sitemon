"""Relays, signed commands, spoken alerts and SMS."""

from __future__ import annotations

import base64
import logging
import dataclasses
import os
import socket
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestClient, TestServer

from sitemon import SigningKey
from sitemon._commands import Command
from sitemon.agent import runtime
from sitemon.agent.ami import AmiClient, AmiError
from sitemon.agent.announce import AnnounceError, Announcer
from sitemon.agent.relays import RelayBank, RelayError
from sitemon.agent.runtime import Agent, NoMeshResolver
from sitemon.config import (
    _control_hub,
    AnnounceConfig,
    ChannelConfig,
    ConfigError,
    ControlConfig,
    OperatorConfig,
    RelayConfig,
    RuleConfig,
    load_agent_config,
    load_hub_config,
)
from sitemon.hub import channels
from sitemon.hub.db import HubDB
from sitemon.hub.server import Hub

from .conftest import FakeAmi, make_hub_config
from .test_agent import agent_cfg, recorded

NOW = 1_800_000_000.0


class FakeOutput:
    def __init__(self, pin: int, active_low: bool) -> None:
        self.pin, self.active_low = pin, active_low
        self.level = False
        self.changes: list[bool] = []
        self.closed = False

    def on(self) -> None:
        self.level = True
        self.changes.append(True)

    def off(self) -> None:
        self.level = False
        self.changes.append(False)

    def close(self) -> None:
        self.closed = True


class Outputs:
    """Builds FakeOutputs and remembers them by pin."""

    def __init__(self) -> None:
        self.by_pin: dict[int, FakeOutput] = {}

    def __call__(self, pin: int, active_low: bool) -> FakeOutput:
        self.by_pin[pin] = FakeOutput(pin, active_low)
        return self.by_pin[pin]


RADIO = RelayConfig(name="radio", pin=5, mode="pulse", active_low=True, pulse=10, max_on=3600)
LIGHTS = RelayConfig(name="lights", pin=6, mode="switch", active_low=False, pulse=10, max_on=600)


def test_command_signature_covers_everything() -> None:
    op = SigningKey.generate()
    cmd = Command.create("hilltop", "relay", {"relay": "radio", "action": "pulse"}).signed(op)
    assert cmd.signer([op.public_key()]) == op.public_key()
    for change in (
        {"site": "ridge"},
        {"args": {"relay": "lights", "action": "on"}},
        {"expires": cmd.expires + 600},
    ):
        tampered = Command.from_dict({**cmd.to_dict(), **change})
        assert tampered.signer([op.public_key()]) is None
    assert cmd.signer([SigningKey.generate().public_key()]) is None


def test_command_lifetime_and_validation() -> None:
    cmd = Command.create("s", "relay", {}, lifetime=120, now=NOW)
    assert cmd.problem(NOW + 60) is None
    assert cmd.problem(NOW + 121) == "expired"
    assert "future" in str(cmd.problem(NOW - 600))
    with pytest.raises(ValueError):
        Command.create("s", "relay", {}, lifetime=3600)
    with pytest.raises(ValueError):
        Command.create("s", "reboot", {})
    for bad in (
        {},
        {**cmd.to_dict(), "nonce": "x"},
        {**cmd.to_dict(), "kind": "shell"},
        {**cmd.to_dict(), "expires": "soon"},
        [1],
    ):
        with pytest.raises(ValueError):
            Command.from_dict(bad)


def test_pulse_relay_switches_itself_off() -> None:
    t = [0.0]
    out = Outputs()
    bank = RelayBank([RADIO, LIGHTS], out, clock=lambda: t[0])
    assert out.by_pin[5].changes == [False]  # starts off
    assert bank.run("radio", "pulse") == "radio on for 10 s"
    assert bank.states() == {"radio": True, "lights": False}
    t[0] = 9.9
    assert not bank.tick()
    t[0] = 10
    assert bank.tick() and bank.states()["radio"] is False
    assert bank.run("radio", "pulse", seconds=99) == "radio on for 10 s"  # capped at its limit


def test_switch_relay_has_a_time_limit() -> None:
    t = [0.0]
    bank = RelayBank([LIGHTS], Outputs(), clock=lambda: t[0])
    assert bank.run("lights", "on") == "lights on for 600 s"
    t[0] = 300
    assert bank.run("lights", "off") == "lights off"
    bank.run("lights", "on", seconds=60)
    t[0] = 361
    bank.tick()
    assert bank.states() == {"lights": False}


@pytest.mark.parametrize(
    "relay, action, seconds, msg",
    [
        ("radio", "on", None, "pulse relay"),
        ("lights", "pulse", None, "switch relay"),
        ("heater", "on", None, "no relay"),
        ("radio", "pulse", -1.0, "positive"),
        ("radio", "pulse", float("nan"), "positive"),
    ],
)
def test_relay_refusals(relay: str, action: str, seconds: float | None, msg: str) -> None:
    bank = RelayBank([RADIO, LIGHTS], Outputs())
    with pytest.raises(RelayError, match=msg):
        bank.run(relay, action, seconds)
    assert not any(bank.states().values())


def test_close_turns_everything_off() -> None:
    out = Outputs()
    bank = RelayBank([RADIO, LIGHTS], out)
    bank.run("lights", "on")
    bank.close()
    assert not out.by_pin[6].level and out.by_pin[6].closed


def test_keepalive_toggles_only_while_healthy(tmp_path: Path, key: SigningKey) -> None:
    out = Outputs()
    cfg = agent_cfg(
        tmp_path,
        ["http://hub"],
        control=ControlConfig(["http://hub"], [key.public_key().to_b64()], [], 22),
    )
    ag = Agent(cfg, key, [], output=out)
    pin = out.by_pin[22]
    for _ in range(3):
        ag.relays_once()
    ag.last_sample -= 3600  # sampling has wedged
    for _ in range(3):
        ag.relays_once()
    assert pin.changes == [True, False, True]
    ag.log.close()


def test_a_relays_own_watchdog_runs_only_while_it_is_on(tmp_path: Path, key: SigningKey) -> None:
    out = Outputs()
    radio = dataclasses.replace(RADIO, watchdog_pin=23)
    cfg = agent_cfg(
        tmp_path,
        ["http://hub"],
        relays=[radio, LIGHTS],
        control=ControlConfig(["http://hub"], [key.public_key().to_b64()], [], None),
    )
    ag = Agent(cfg, key, [], output=out)
    assert ag.relays is not None
    wd = out.by_pin[23]
    ag.relays_once()
    assert wd.changes == []  # off: nothing to keep alive
    ag.relays.run("radio", "pulse")
    ag.relays_once()
    ag.relays_once()
    assert wd.changes == [True, False]
    ag.last_sample -= 3600  # sampling has wedged: the watchdog stops with the relay still on
    ag.relays_once()
    assert wd.changes == [True, False] and ag.relays.relays["radio"].is_on
    ag._power_down(close=True)
    assert wd.closed
    ag.log.close()


class Dns(AbstractResolver):
    """Stands in for DNS, answering every name with ``answers``."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        return [
            ResolveResult(hostname=host, host=a, port=port, family=family, proto=0, flags=0)
            for a in self.answers
        ]

    async def close(self) -> None:
        pass


async def test_mesh_addresses_are_dropped_from_dns_answers_for_control_hubs() -> None:
    hubs = {"hub.example.org"}
    mixed = NoMeshResolver(hubs, Dns(["10.54.3.2", "203.0.113.7"]))
    assert [r["host"] for r in await mixed.resolve("hub.example.org", 443)] == ["203.0.113.7"]
    for answer in ("10.54.3.2", "::ffff:10.54.3.2"):
        with pytest.raises(OSError, match="only to AREDN mesh addresses"):
            await NoMeshResolver(hubs, Dns([answer])).resolve("HUB.example.org.", 443)
    # A hub that only takes uploads may well be on the mesh.
    mesh_hub = await NoMeshResolver(hubs, Dns(["10.54.3.2"])).resolve("hub.local.mesh", 443)
    assert [r["host"] for r in mesh_hub] == ["10.54.3.2"]


class Rig:
    """A hub with one operator and an agent with relays that takes commands from it."""

    def __init__(
        self, tmp: Path, *, announce: AnnounceConfig | None = None, ami: AmiClient | None = None
    ) -> None:
        self.site_key = SigningKey.generate()
        self.op = SigningKey.generate()
        self.hub_key = SigningKey.generate()
        key_file = tmp / "hub.key"
        key_file.write_text(self.hub_key.to_hex())
        self.hub = Hub(
            make_hub_config(
                {"s1": self.site_key},
                operators=[OperatorConfig("me", self.op.public_key().to_b64(), ["s*"])],
                key_path=str(key_file),
            ),
            db=HubDB(":memory:"),
        )
        self.out = Outputs()
        self.tmp = tmp
        self.announce = announce
        self.ami = ami

    def agent(self, url: str, control: bool = True) -> Agent:
        cfg = agent_cfg(
            self.tmp,
            [url],
            relays=[RADIO, LIGHTS],
            announce=self.announce,
            control=ControlConfig(
                [url] if control else [],
                [self.op.public_key().to_b64()],
                [self.hub_key.public_key().to_b64()],
                None,
            ),
        )
        if self.ami is not None:
            from sitemon.config import AmiConfig

            cfg.ami = AmiConfig("127.0.0.1", 1, "u", "p", 1999, 15, "")
        ag = Agent(cfg, self.site_key, [], output=self.out, ami=self.ami)
        if self.announce is not None and self.ami is not None:
            ag.announcer = Announcer(self.announce, self.ami, 1999, str(self.tmp / "announce"))
        return ag

    def command(self, **args: Any) -> Command:
        return Command.create("s1", "relay", args).signed(self.op)


async def test_relay_command_from_operator_to_relay_and_back(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    async with TestClient(TestServer(rig.hub.make_app())) as client:
        url = str(client.make_url("")).rstrip("/")
        ag = rig.agent(url)
        cmd = rig.command(relay="radio", action="pulse", seconds=5)
        r = await client.post("/api/commands", json=cmd.to_dict())
        assert r.status == 202
        hub = ag.hubs[url]
        await ag.poll_commands(hub)
        assert rig.out.by_pin[5].level  # the radio is being power-cycled
        assert (await (await client.get(f"/api/commands/{cmd.nonce}")).json())["status"] == "sent"
        ag.log.flush()
        assert await ag.sync_hub(hub)  # the result goes back with the next upload
        st = await (await client.get(f"/api/commands/{cmd.nonce}")).json()
        assert st["status"] == "done" and st["detail"] == "radio on for 5 s"
        site = await (await client.get("/api/site/s1")).json()
        assert site["commands"][0]["status"] == "done"
        assert site["latest"]["relay.radio"]["value"] is True
        # The hub resends until it hears back; the agent runs a nonce only once.
        rig.out.by_pin[5].changes.clear()
        await ag.handle_command(cmd.to_dict())
        assert rig.out.by_pin[5].changes == []
        if ag._session:
            await ag._session.close()
    ag.log.close()


async def test_a_control_hub_that_resolves_onto_the_mesh_is_not_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(tmp_path)
    async with TestClient(TestServer(rig.hub.make_app())) as client:
        url = f"http://hub.example:{client.port}"
        cmd = rig.command(relay="radio", action="pulse")
        assert (await client.post("/api/commands", json=cmd.to_dict())).status == 202
        ag = rig.agent(url)
        for answer, arrived in (("10.54.3.2", 0), ("127.0.0.1", 1)):
            monkeypatch.setattr(aiohttp, "DefaultResolver", lambda a=answer: Dns([a]))
            with caplog.at_level(logging.INFO, logger="sitemon.agent"):
                assert await ag.poll_commands(ag.hubs[url]) == arrived
            assert ag._session is not None
            await ag._session.close()
        assert "resolves only to AREDN mesh addresses" in caplog.text  # the log says why
        assert rig.out.by_pin[5].level  # reached over the non-mesh address only
    ag.log.close()


async def test_a_poll_is_not_redirected(tmp_path: Path, key: SigningKey) -> None:
    hits: list[str] = []

    async def elsewhere(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.json_response({"commands": []})

    other = web.Application()
    other.router.add_post("/api/commands/poll", elsewhere)
    async with TestServer(other) as there:

        async def bounce(_: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(str(there.make_url("/api/commands/poll")))

        hub = web.Application()
        hub.router.add_post("/api/commands/poll", bounce)
        async with TestServer(hub) as here:
            url = str(here.make_url("")).rstrip("/")
            control = ControlConfig([url], [key.public_key().to_b64()], [], None)
            ag = Agent(agent_cfg(tmp_path, [url], control=control), key, [])
            assert await ag.poll_commands(ag.hubs[url]) == 0
            assert hits == []
            assert ag._session is not None
            await ag._session.close()
            ag.log.close()


def test_each_on_period_starts_the_relays_watchdog_with_a_rising_edge(
    tmp_path: Path, key: SigningKey
) -> None:
    out = Outputs()
    radio = dataclasses.replace(RADIO, watchdog_pin=23, pulse=1.0)
    control = ControlConfig(["http://hub"], [key.public_key().to_b64()], [], None)
    ag = Agent(
        agent_cfg(tmp_path, ["http://hub"], relays=[radio], control=control), key, [], output=out
    )
    assert ag.relays is not None
    now = [100.0]
    ag.relays.clock = lambda: now[0]
    wd = out.by_pin[23]
    for _ in range(4):
        wd.changes.clear()
        ag.relays.run("radio", "pulse")
        now[0] += 0.75
        ag.relays_once()
        assert wd.changes[0] is True  # the first tick after the command
        now[0] += 1.5
        ag.relays_once()
        ag.relays_once()
        assert not wd.level  # rests low between pulses
    ag.log.close()


async def test_hub_refuses_commands_it_cannot_vouch_for(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    async with TestClient(TestServer(rig.hub.make_app())) as client:
        stranger = Command.create("s1", "relay", {"relay": "radio", "action": "pulse"})
        cases = [
            (stranger.signed(SigningKey.generate()), 403),
            (Command.create("s1", "announce", {"text": "hi"}).signed(rig.op), 403),
            (Command.create("ridge", "relay", {}).signed(rig.op), 404),
            (Command.create("s1", "relay", {}, lifetime=1, now=NOW).signed(rig.op), 400),
        ]
        for cmd, status in cases:
            assert (await client.post("/api/commands", json=cmd.to_dict())).status == status
        ok = rig.command(relay="radio", action="pulse")
        assert (await client.post("/api/commands", json=ok.to_dict())).status == 202
        assert (await client.post("/api/commands", json=ok.to_dict())).status == 409
        assert (await client.post("/api/commands", data=b"nope")).status == 400
        # Polling needs the site's own signature.
        assert (await client.post("/api/commands/poll", json={"site": "s1"})).status == 400
        assert (await client.get("/api/commands/" + "0" * 32)).status == 404


@pytest.mark.parametrize(
    "why", ["other site", "hub key on a relay", "operator key", "expired", "clock unset"]
)
async def test_agent_refuses_bad_commands(
    tmp_path: Path, why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    args = {"relay": "radio", "action": "pulse"}
    if why == "other site":
        cmd = Command.create("ridge", "relay", args).signed(rig.op)
    elif why == "hub key on a relay":
        cmd = Command.create("s1", "relay", args).signed(rig.hub_key)
    elif why == "operator key":  # an operator can't make the repeater talk
        cmd = Command.create("s1", "announce", {"text": "hi"}).signed(rig.op)
    elif why == "expired":
        cmd = Command.create("s1", "relay", args, lifetime=1, now=NOW - 60).signed(rig.op)
    else:
        monkeypatch.setattr(runtime, "SANE_CLOCK", 4_000_000_000.0)
        cmd = rig.command(**args)
    await ag.handle_command(cmd.to_dict())
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data["ok"] is False and ev.data["nonce"] == cmd.nonce
    assert not any(rig.out.by_pin[p].level for p in (5, 6))
    ag.log.close()


async def test_commands_only_come_from_control_hubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://mesh-hub", control=False)
    polled: list[str] = []

    async def fake_poll(hub: Any) -> None:
        polled.append(hub.url)

    async def fake_sync(hub: Any, batch: int = 500) -> bool:
        ag.stop()
        return True

    monkeypatch.setattr(ag, "poll_commands", fake_poll)
    monkeypatch.setattr(ag, "sync_hub", fake_sync)
    await ag._sync_loop(ag.hubs["http://mesh-hub"])
    assert polled == []
    ag.log.close()


def test_nonces_survive_a_restart(tmp_path: Path, key: SigningKey) -> None:
    from sitemon.agent.store import EventLog

    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[])
    assert log.first_use("a" * 32, NOW)
    log.close()
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[])
    assert not log.first_use("a" * 32, NOW)
    log.close()


def say_cfg(min_gap: float = 60) -> AnnounceConfig:
    # Stand-ins for espeak-ng and sox that write the files the real ones would.
    write = "import sys; open(sys.argv[1], 'w').write(sys.argv[2])"
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    return AnnounceConfig(
        say_command=[sys.executable, "-c", write, "{wav}", "{text}"],
        convert_command=[sys.executable, "-c", copy, "{wav}", "{out}"],
        min_gap=min_gap,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="app_rpt plays POSIX paths; sitemon runs on Linux"
)
async def test_announcement_is_spoken_through_app_rpt(tmp_path: Path, fake_ami: FakeAmi) -> None:
    ami = AmiClient(
        "127.0.0.1", fake_ami.port, "sitemon", "s3cret", sound_dir=str(tmp_path / "announce")
    )
    rig = Rig(tmp_path, announce=say_cfg(), ami=ami)
    ag = rig.agent("http://hub")
    cmd = Command.create("s1", "announce", {"text": "Hilltop. Battery low"}).signed(rig.hub_key)
    await ag.handle_command(cmd.to_dict())
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data == {
        "nonce": cmd.nonce,
        "kind": "announce",
        "ok": True,
        "detail": "announced: Hilltop. Battery low",
    }
    played = [a["Command"] for a in fake_ami.actions if a.get("Action") == "Command"]
    base = str(tmp_path / "announce" / cmd.nonce)
    assert played == [f"rpt localplay 1999 {base}"]
    assert (tmp_path / "announce" / f"{cmd.nonce}.wav").read_text() == "Hilltop. Battery low"
    await ami.close()
    ag.log.close()


class RecordingAmi(AmiClient):
    """An AMI client that records what it would play instead of connecting."""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 1, "u", "p")
        self.played: list[tuple[int, str]] = []

    @property
    def connected(self) -> bool:
        return True

    async def play(self, node: int, path: str) -> None:
        self.played.append((node, path))


async def test_announcer_speaks_converts_and_plays(tmp_path: Path) -> None:
    ami = RecordingAmi()
    await Announcer(say_cfg(), ami, 1999, str(tmp_path)).say("Hilltop. Door open", "a" * 32)
    base = os.path.join(os.path.abspath(tmp_path), "a" * 32)
    assert ami.played == [(1999, base)]
    assert (tmp_path / f"{'a' * 32}.wav").read_text() == "Hilltop. Door open"


async def test_announcer_keeps_a_gap_and_reports_failures(tmp_path: Path) -> None:
    t = [0.0]
    a = Announcer(say_cfg(min_gap=60), RecordingAmi(), 1999, str(tmp_path), clock=lambda: t[0])
    await a.say("one", "a" * 32)
    t[0] = 30
    with pytest.raises(AnnounceError, match="min_gap"):
        await a.say("two", "b" * 32)
    broken = say_cfg()
    broken.say_command = [
        sys.executable,
        "-c",
        "import sys; sys.exit('no voice')",
        "{wav}",
        "{text}",
    ]
    with pytest.raises(AnnounceError, match="no voice"):
        await Announcer(broken, RecordingAmi(), 1999, str(tmp_path)).say("x", "c" * 32)
    missing = say_cfg()
    missing.say_command = ["no-such-tts-program", "{wav}", "{text}"]
    with pytest.raises(AnnounceError, match="cannot run"):
        await Announcer(missing, RecordingAmi(), 1999, str(tmp_path)).say("x", "d" * 32)


@pytest.mark.parametrize(
    "path",
    ["/var/lib/sitemon/../../etc/passwd", "/tmp/a b", "relative/path", "/tmp/x; core stop now"],
)
async def test_only_safe_files_can_be_played(fake_ami: FakeAmi, path: str) -> None:
    c = AmiClient("127.0.0.1", fake_ami.port, "sitemon", "s3cret")
    await c.connect()
    with pytest.raises(AmiError, match="refusing"):
        await c.play(1999, path)
    await c.close()


def test_hub_queues_one_announcement_per_gap(tmp_path: Path) -> None:
    from .test_hub import Site, feed

    rig = Rig(tmp_path)
    s = Site()
    s.key = rig.site_key
    rig.hub.cfg.rules = [
        RuleConfig(
            name="Battery low",
            type="threshold",
            metric="batt.v",
            sites=["*"],
            below=12.2,
            critical_below=11.8,
            unit="V",
            announce=True,
        ),
        RuleConfig(name="Door open", type="state", metric="door.open", sites=["*"], bad=True),
    ]
    feed(rig.hub, s, "state", "door", {"open": True}, NOW)  # not announced: no announce flag
    feed(rig.hub, s, "reading", "batt", {"v": 12.0}, NOW)  # warn only: not announced
    rig.hub.evaluate("s1", NOW)
    assert rig.hub.db.q("SELECT nonce FROM commands") == []
    feed(rig.hub, s, "reading", "batt", {"v": 11.5}, NOW + 30)
    rig.hub.evaluate("s1", NOW + 30)
    (row,) = rig.hub.db.q("SELECT body FROM commands")
    import json

    cmd = Command.from_dict(json.loads(row["body"]))
    assert cmd.kind == "announce" and cmd.signer([rig.hub_key.public_key()]) is not None
    assert cmd.args["text"] == "S1. Battery low: 11.5 V below 11.8 V"
    # Resolves and comes back within the gap: no second announcement.
    feed(rig.hub, s, "reading", "batt", {"v": 12.6}, NOW + 60)
    rig.hub.evaluate("s1", NOW + 60)
    feed(rig.hub, s, "reading", "batt", {"v": 11.5}, NOW + 90)
    rig.hub.evaluate("s1", NOW + 90)
    assert len(rig.hub.db.q("SELECT nonce FROM commands")) == 1


# SMS


async def test_sms_through_twilio() -> None:
    got: list[tuple[str, str, Any]] = []
    fail = {"status": 0}

    async def handler(req: web.Request) -> web.Response:
        if fail["status"]:
            return web.Response(status=fail["status"])
        got.append((req.path, req.headers["Authorization"], await req.post()))
        return web.Response(status=201)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    async with TestServer(app) as srv:
        ch = ChannelConfig(
            type="sms",
            name="sms",
            min_severity="critical",
            url=str(srv.make_url("")).rstrip("/"),
            account_sid="AC123",
            token="secret",
            sender="+15550001111",
            to=["+15550002222"],
        )
        import aiohttp

        async with aiohttp.ClientSession() as session:
            await channels.send(
                session, ch, "CRITICAL: Hilltop", "x" * 400, "critical", "+15550002222"
            )
            fail["status"] = 400
            with pytest.raises(channels.ChannelError) as bad:
                await channels.send(session, ch, "t", "b", "critical", "+15550002222")
            fail["status"] = 503
            with pytest.raises(channels.ChannelError) as busy:
                await channels.send(session, ch, "t", "b", "critical", "+15550002222")
    (path, auth, form) = got[0]
    assert path == "/2010-04-01/Accounts/AC123/Messages.json"
    assert auth == "Basic " + base64.b64encode(b"AC123:secret").decode()
    assert form["To"] == "+15550002222" and form["From"] == "+15550001111"
    assert len(form["Body"]) == 306 and form["Body"].startswith("CRITICAL: Hilltop: ")
    assert bad.value.permanent and not busy.value.permanent


def write(tmp: Path, text: str) -> str:
    p = tmp / "c.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_agent_control_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    op = SigningKey.generate().public_key().to_b64()
    base = (
        f'[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\noperators=["{op}"]\nwatchdog_pin=22\n'
        '[[relays]]\nname="radio"\npin=5\npulse="20s"\n'
        '[[relays]]\nname="lights"\npin=6\nmode="switch"\nmax_on="2h"\nactive_low=false\n'
        "transmitter=false\n"
    )
    cfg = load_agent_config(write(tmp_path, base))
    assert cfg.control is not None and cfg.control.watchdog_pin == 22
    assert [(r.name, r.mode, r.pulse, r.max_on, r.active_low) for r in cfg.relays] == [
        ("radio", "pulse", 20, 120, True),
        ("lights", "switch", 10, 7200, False),
    ]
    for old, new, msg in [
        ('[control]\nhubs=["https://hub.example"]', '[control]\nhubs=["https://other"]', "not in"),
        (f'operators=["{op}"]', 'operators=["junk"]', "not a public key"),
        ("pin=6", "pin=5", "already used"),
        ("pin=6", "pin=22", "already used"),
        ('mode="switch"', 'mode="toggle"', "pulse or switch"),
        (f'operators=["{op}"]', "operators=[]", "operator key"),
    ]:
        with pytest.raises(ConfigError, match=msg):
            load_agent_config(write(tmp_path, base.replace(old, new)))


def test_announce_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S", "x")
    hk = SigningKey.generate().public_key().to_b64()
    base = (
        '[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\nhub_keys=["{hk}"]\n'
        '[ami]\nnode=1999\nsecret_env="S"\n[announce]\nenabled=true\n'
    )
    cfg = load_agent_config(write(tmp_path, base))
    assert cfg.announce is not None and cfg.announce.say_command[0] == "espeak-ng"
    with pytest.raises(ConfigError, match="needs \\[ami\\]"):
        load_agent_config(write(tmp_path, base.replace('[ami]\nnode=1999\nsecret_env="S"\n', "")))
    with pytest.raises(ConfigError, match="hub_keys"):
        load_agent_config(write(tmp_path, base.replace(f'hub_keys=["{hk}"]', "hub_keys=[]")))
    with pytest.raises(ConfigError, match="must use \\{text\\}"):
        load_agent_config(write(tmp_path, base + 'say_command=["espeak-ng", "{wav}"]\n'))


def test_hub_command_and_sms_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK", "t")
    op = SigningKey.generate().public_key().to_b64()
    text = (
        f'[hub]\nkey="/etc/sitemon/hub.key"\n[[operators]]\nname="me"\nkey="{op}"\n'
        'sites=["hill*"]\n'
        '[[rules]]\nname="Low"\nmetric="batt.v"\nbelow=12\nannounce=true\n'
        '[[channels]]\ntype="sms"\naccount_sid="AC1"\nauth_token_env="TOK"\n'
        'from="+1555"\nto=["+1666"]\n'
    )
    cfg = load_hub_config(write(tmp_path, text))
    assert cfg.operators[0].sites == ["hill*"] and cfg.rules[0].announce
    ch = cfg.channels[0]
    assert (ch.type, ch.account_sid, ch.token, ch.url) == (
        "sms",
        "AC1",
        "t",
        "https://api.twilio.com",
    )
    with pytest.raises(ConfigError, match="need \\[hub\\] key"):
        load_hub_config(write(tmp_path, text.replace('key="/etc/sitemon/hub.key"\n', "")))
    with pytest.raises(ConfigError, match="account_sid"):
        load_hub_config(write(tmp_path, text.replace('account_sid="AC1"\n', "")))


def test_only_the_announcement_folder_can_be_played() -> None:
    from sitemon.agent.ami import _allowed_command

    folder = "/var/lib/sitemon/announce"
    assert _allowed_command(f"rpt localplay 1999 {folder}/{'a' * 32}", folder)
    for bad in (
        f"rpt localplay 1999 {folder}/../../../etc/passwd",
        "rpt localplay 1999 /etc/passwd",
        f"rpt localplay 1999 {folder}x/a",
        f"rpt localplay 1999 {folder}/a b",
    ):
        assert not _allowed_command(bad, folder)
    assert not _allowed_command(f"rpt localplay 1999 {folder}/a", None)  # announcements off


class DownAmi(AmiClient):
    """Asterisk isn't running."""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 1, "u", "p", sound_dir="/var/lib/sitemon/announce")

    @property
    def connected(self) -> bool:
        return False

    async def connect(self) -> None:
        raise ConnectionRefusedError("connection refused")


async def test_announcement_with_asterisk_down_is_refused_not_fatal(tmp_path: Path) -> None:
    rig = Rig(tmp_path, announce=say_cfg(), ami=DownAmi())
    ag = rig.agent("http://hub")
    cmd = Command.create("s1", "announce", {"text": "Battery low"}).signed(rig.hub_key)
    await ag.handle_command(cmd.to_dict())
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data["ok"] is False and "playing it failed" in ev.data["detail"]
    ag.log.close()


async def test_announcement_text_is_cleaned_on_the_site(tmp_path: Path) -> None:
    ami = RecordingAmi()
    a = Announcer(say_cfg(), ami, 1999, str(tmp_path))
    assert await a.say("--version; reboot now `x`", "a" * 32) == "announced: version reboot now x"
    with pytest.raises(AnnounceError, match="nothing to say"):
        await a.say("---", "b" * 32)


def test_stop_switches_relays_off_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    assert ag.relays is not None
    ag.relays.run("lights", "on")
    ag.stop()
    assert not rig.out.by_pin[6].level
    ag.log.close()


async def test_stopping_agent_refuses_commands(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    ag.stop()
    await ag.handle_command(rig.command(relay="lights", action="on").to_dict())
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data["ok"] is False and "stopping" in ev.data["detail"]
    assert not rig.out.by_pin[6].level
    ag.log.close()


def recorded_all(ag: Agent) -> list[Any]:
    from sitemon.agent.store import EventLog

    log = EventLog(str(Path(ag.cfg.data_dir) / "events.db"), "s1", ag.key, hubs=[])
    events = log.unsent("nobody", 10_000)
    log.close()
    return events


async def test_relays_off_first_when_the_agent_crashes(tmp_path: Path) -> None:
    import asyncio
    from typing import Literal

    class Broken:
        name = "batt"
        kind: Literal["reading", "state"] = "reading"

        def read(self) -> dict[str, Any]:
            raise RuntimeError("driver bug")

        def close(self) -> None:
            raise OSError("bus gone")  # a later cleanup step failing

    rig = Rig(tmp_path)
    ag = rig.agent("http://127.0.0.1:9")
    ag.sensors = [Broken()]
    assert ag.relays is not None
    ag.relays.run("lights", "on")
    with pytest.raises(RuntimeError, match="driver bug"):
        await asyncio.wait_for(ag.run(), 30)
    assert not rig.out.by_pin[6].level and rig.out.by_pin[6].closed


async def test_relay_state_is_reported_at_startup(tmp_path: Path) -> None:
    import asyncio

    rig = Rig(tmp_path)
    ag = rig.agent("http://127.0.0.1:9")
    task = asyncio.create_task(ag.run())
    await asyncio.sleep(0.2)
    ag.stop()
    await asyncio.wait_for(task, 30)
    states = [e.data for e in recorded_all(ag) if e.kind == "state" and e.source == "relay"]
    assert states[0] == {"radio": False, "lights": False}


def test_one_failing_output_does_not_keep_others_on() -> None:
    from sitemon.agent.relays import Relay, _switch_off

    class Stuck(FakeOutput):
        def off(self) -> None:
            raise OSError("pin busy")

    lamp = FakeOutput(6, False)
    radio = Relay(RADIO, Stuck(5, True))
    lights = Relay(LIGHTS, lamp)
    lamp.on()
    lights.until = 100.0
    with pytest.raises(OSError, match="pin busy"):
        _switch_off([radio, lights])
    assert not lamp.level and lights.until is None


def test_relay_loop_enforces_the_limit(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    t = [0.0]
    assert ag.relays is not None
    ag.relays.clock = lambda: t[0]
    ag.relays.run("radio", "pulse")
    t[0] = 11.0
    ag.relays_once()
    assert not rig.out.by_pin[5].level
    assert [e.data for e in recorded(ag) if e.source == "relay"][-1]["radio"] is False
    ag.log.close()


@pytest.mark.parametrize("seconds", [float("nan"), -float("inf"), -5, 0, "5", True, 10**400])
async def test_bad_seconds_are_refused(tmp_path: Path, seconds: Any) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    await ag.handle_command(rig.command(relay="radio", action="pulse", seconds=seconds).to_dict())
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data["ok"] is False
    assert not rig.out.by_pin[5].level
    ag.log.close()


async def test_unencodable_command_is_refused_not_fatal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ag = rig.agent("http://hub")
    raw = Command.create("s1", "relay", {"relay": "\ud800"}).to_dict() | {"sig": "x"}
    await ag.handle_command(raw)
    (ev,) = [e for e in recorded(ag) if e.kind == "command"]
    assert ev.data["ok"] is False and "not signed" in ev.data["detail"]
    ag.log.close()


def test_lifetime_cap_is_enforced() -> None:
    from dataclasses import replace

    cmd = Command.create("s", "relay", {}, now=NOW)
    assert replace(cmd, expires=cmd.issued + 3600).problem(NOW + 10) == "lifetime too long"


async def test_operator_limited_to_their_sites(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    key = rig.op.public_key()
    rig.hub.operators = [(OperatorConfig("me", key.to_b64(), ["ridge*"]), key)]
    async with TestClient(TestServer(rig.hub.make_app())) as client:
        cmd = rig.command(relay="radio", action="pulse")
        assert (await client.post("/api/commands", json=cmd.to_dict())).status == 403


async def test_big_bodies_and_replayed_polls_are_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    async with TestClient(TestServer(rig.hub.make_app())) as client:
        big = b'{"pad": "' + b"x" * 20000 + b'"}'
        for path in ("/api/commands", "/api/commands/poll"):
            r = await client.post(path, data=big)
            assert r.status == 400 and "too large" in (await r.json())["error"]
        ag = rig.agent(str(client.make_url("")).rstrip("/"))
        env = ag._envelope([])
        assert (await client.post("/api/commands/poll", json=env)).status == 200
        assert (await client.post("/api/commands/poll", json=env)).status == 409
    ag.log.close()


def test_resolve_and_reminders_are_not_announced(tmp_path: Path) -> None:
    from .test_hub import Site, feed

    rig = Rig(tmp_path)
    s = Site()
    s.key = rig.site_key
    rig.hub.cfg.rules = [
        RuleConfig(
            name="Battery low",
            type="threshold",
            metric="batt.v",
            sites=["*"],
            critical_below=11.8,
            unit="V",
            announce=True,
        )
    ]
    rig.hub.cfg.announce_gap = 0
    rig.hub.cfg.remind_critical = 60
    feed(rig.hub, s, "reading", "batt", {"v": 11.5}, NOW)
    rig.hub.evaluate("s1", NOW)
    feed(rig.hub, s, "reading", "batt", {"v": 11.5}, NOW + 120)
    assert [n.event for n in rig.hub.evaluate("s1", NOW + 120)] == ["remind"]
    feed(rig.hub, s, "reading", "batt", {"v": 12.8}, NOW + 180)
    assert [n.event for n in rig.hub.evaluate("s1", NOW + 180)] == ["resolve"]
    assert len(rig.hub.db.q("SELECT nonce FROM commands")) == 1


async def test_each_sms_number_is_its_own_message(tmp_path: Path) -> None:
    from .test_hub import Site, feed

    got: list[str] = []

    async def handler(req: web.Request) -> web.Response:
        to = str((await req.post())["To"])
        if to == "+1bad":
            return web.Response(status=400)
        got.append(to)
        return web.Response(status=201)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    async with TestServer(app) as srv:
        rig = Rig(tmp_path)
        s = Site()
        s.key = rig.site_key
        rig.hub.cfg.channels = [
            ChannelConfig(
                type="sms",
                name="sms",
                min_severity="warn",
                url=str(srv.make_url("")).rstrip("/"),
                account_sid="AC1",
                token="t",
                sender="+1000",
                to=["+1bad", "+1good"],
            )
        ]
        feed(rig.hub, s, "reading", "batt", {"v": 12.0}, NOW)
        rig.hub.evaluate("s1", NOW)
        assert await rig.hub.flush_outbox(NOW) == 1
        assert await rig.hub.flush_outbox(NOW + 400) == 0  # nothing to resend
        await rig.hub.http().close()
    assert got == ["+1good"]
    bad = rig.hub.db.q1("SELECT last_error FROM outbox WHERE target='+1bad'")
    assert bad is not None and bad["last_error"].startswith("gave up")


async def test_sms_rate_limit_is_retried() -> None:
    import aiohttp

    async def handler(req: web.Request) -> web.Response:
        return web.Response(status=429)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    async with TestServer(app) as srv:
        ch = ChannelConfig(
            type="sms",
            name="sms",
            min_severity="warn",
            url=str(srv.make_url("")).rstrip("/"),
            account_sid="AC1",
            token="t",
            sender="+1000",
            to=["+1"],
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(channels.ChannelError) as e:
                await channels.send(session, ch, "t", "b", "warn", "+1")
    assert not e.value.permanent


def test_old_hub_database_gets_the_new_column(tmp_path: Path) -> None:
    import sqlite3

    path = str(tmp_path / "hub.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY, channel TEXT NOT NULL, "
        "created REAL NOT NULL, next_try REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, "
        "severity TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, done REAL, "
        "last_error TEXT)"
    )
    con.execute(
        "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
        "VALUES ('d', 1, 1, 'warn', 't', 'b')"
    )
    con.commit()
    con.close()
    row = HubDB(path).q1("SELECT target FROM outbox")
    assert row is not None and row["target"] == ""


@pytest.mark.parametrize(
    "old, new, msg",
    [
        ('pulse="20s"', 'pulse="2d"', "at most a day"),
        ("pin=5", "pin=99", "0-27"),
        ("watchdog_pin=22", "watchdog_pin=true", "0-27"),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["http://hub.example"]\n[control]\nhubs=["http://hub.example"]',
            "must be https",
        ),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["https://hub.local.mesh"]\n[control]\nhubs=["https://hub.local.mesh"]',
            "on the AREDN mesh",
        ),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["Https://Hub.Local.Mesh.:8443/"]\n[control]\nhubs=["Https://Hub.Local.Mesh.:8443/"]',
            "on the AREDN mesh",
        ),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["https://hub.mesh"]\n[control]\nhubs=["https://hub.mesh"]',
            "on the AREDN mesh",
        ),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["https://myhub:8470"]\n[control]\nhubs=["https://myhub:8470"]',
            "a bare name",
        ),
        (
            'hubs=["https://hub.example"]\n[control]\nhubs=["https://hub.example"]',
            'hubs=["https://10.54.3.2"]\n[control]\nhubs=["https://10.54.3.2"]',
            "on the AREDN mesh",
        ),
        (
            'pulse="20s"',
            'pulse="20s"\nwatchdog_pin=5',
            "GPIO5 is already used",
        ),
        ('pulse="20s"', 'pulse="20s"\nwatchdog_pin=22', "GPIO22 is already used"),
        ('pulse="20s"', 'pulse="171s"\ntransmitter=true', "limited to 170 s"),
        ('pulse="20s"', 'pulse="0s"\ntransmitter=true', "more than 0"),
        ('pulse="20s"', 'pulse="20s"\ntransmitter="yes"', "true or false"),
        ('pulse="20s"', 'pulse="20s"\ntransmiter=true', "unknown setting 'transmiter'"),
    ],
)
def test_control_config_limits(tmp_path: Path, old: str, new: str, msg: str) -> None:
    op = SigningKey.generate().public_key().to_b64()
    base = (
        f'[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\noperators=["{op}"]\nwatchdog_pin=22\n'
        '[[relays]]\nname="radio"\npin=5\npulse="20s"\n'
    )
    load_agent_config(write(tmp_path, base))
    with pytest.raises(ConfigError, match=msg):
        load_agent_config(write(tmp_path, base.replace(old, new)))


def test_transmitter_relay_limits(tmp_path: Path) -> None:
    op = SigningKey.generate().public_key().to_b64()
    text = (
        f'[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\noperators=["{op}"]\n'
        '[[relays]]\nname="tx"\npin=5\nmode="switch"\ntransmitter=true\n'
        '[[relays]]\nname="lights"\npin=6\nmode="switch"\ntransmitter=false\n'
        '[[relays]]\nname="fan"\npin=7\nmode="switch"\n'
    )
    tx, lights, fan = load_agent_config(write(tmp_path, text)).relays
    # Held to the limit unless marked as unable to make the station transmit.
    assert tx.max_on == fan.max_on == 120 and lights.max_on == 3600
    with pytest.raises(ConfigError, match="set transmitter = false"):
        load_agent_config(write(tmp_path, text.replace('name="fan"', 'name="fan"\nmax_on="1h"')))
    for limit in ('max_on="170s"', 'max_on="171s"'):
        relay = text.replace("transmitter=true\n", f"transmitter=true\n{limit}\n")
        if limit.endswith('171s"'):
            with pytest.raises(ConfigError, match="limited to 170 s"):
                load_agent_config(write(tmp_path, relay))
        else:
            assert load_agent_config(write(tmp_path, relay)).relays[0].max_on == 170
    # pulse doesn't apply to a switch relay, so it isn't held to the limit
    relay = text.replace("transmitter=true\n", 'transmitter=true\npulse="5m"\n')
    assert load_agent_config(write(tmp_path, relay)).relays[0].max_on == 120


def test_loopback_control_hub_may_be_http(tmp_path: Path) -> None:
    op = SigningKey.generate().public_key().to_b64()
    for hub in ("http://127.0.0.1:8470", "http://localhost:8470"):
        text = (
            f'[site]\nid="s"\nkey="k"\n[agent]\nhubs=["{hub}"]\n'
            f'[control]\nhubs=["{hub}"]\noperators=["{op}"]\n'
        )
        assert load_agent_config(write(tmp_path, text)).control is not None


def test_control_hubs_that_may_be_the_mesh_are_refused() -> None:
    for url in (
        "https://[::ffff:10.54.3.2]",
        "https://[::ffff:a36:302]:8443/x",
        "https://10.1",
        "https://012.0.0.1",
        "https://myhub",
    ):
        with pytest.raises(ConfigError, match="AREDN mesh"):
            _control_hub(url)
    with pytest.raises(ConfigError, match="must be an https URL"):
        _control_hub("sitemon.example.org")
    for url in ("https://[2001:db8::1]", "https://203.0.113.7", "https://hub.example"):
        _control_hub(url)


def test_a_relays_watchdog_pin_must_be_free(tmp_path: Path) -> None:
    op = SigningKey.generate().public_key().to_b64()
    base = (
        f'[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\noperators=["{op}"]\n'
        '[[sensors]]\ntype="door"\nname="door"\npin=17\n'
        '[[relays]]\nname="radio"\npin=5\nWD\n'
        '[[relays]]\nname="fan"\npin=6\n'
    )
    assert load_agent_config(write(tmp_path, base.replace("WD", "watchdog_pin=24")))
    for taken in (17, 6):
        with pytest.raises(ConfigError, match=f"GPIO{taken} is already used"):
            load_agent_config(write(tmp_path, base.replace("WD", f"watchdog_pin={taken}")))


@pytest.mark.parametrize(
    "cmd, msg",
    [
        ('["sh", "-c", "espeak-ng {text} -w {wav}"]', "not through a shell"),
        ('["espeak-ng", "-v", "{voice}", "-w", "{wav}", "{text}"]', "only \\{text\\}"),
    ],
)
def test_announce_command_templates_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str, msg: str
) -> None:
    monkeypatch.setenv("S", "x")
    hk = SigningKey.generate().public_key().to_b64()
    text = (
        '[site]\nid="s"\nkey="k"\n[agent]\nhubs=["https://hub.example"]\n'
        f'[control]\nhubs=["https://hub.example"]\nhub_keys=["{hk}"]\n'
        '[ami]\nnode=1999\nsecret_env="S"\n'
        f"[announce]\nenabled=true\nsay_command={cmd}\n"
    )
    with pytest.raises(ConfigError, match=msg):
        load_agent_config(write(tmp_path, text))


async def test_relays_go_off_before_the_shutdown_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    rig = Rig(tmp_path)
    ag = rig.agent("http://127.0.0.1:9")
    assert ag.relays is not None
    seen: list[bool] = []

    async def slow_sync(batch: int = 500) -> bool:
        seen.append(rig.out.by_pin[6].level)  # a hung hub would hold this for 30 s
        return True

    monkeypatch.setattr(ag, "sync_once", slow_sync)
    task = asyncio.create_task(ag.run())
    await asyncio.sleep(0.1)
    ag.relays.run("lights", "on")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [False]
