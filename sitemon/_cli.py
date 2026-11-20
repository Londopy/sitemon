"""Command line: ``sitemon keygen|pubkey|agent|hub|check|summary|command|demo``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable

from . import __version__
from ._commands import Command
from ._events import SigningKey
from .config import (
    AgentConfig,
    ChannelConfig,
    ConfigError,
    HubConfig,
    RuleConfig,
    SiteConfig,
    load_agent_config,
    load_hub_config,
)


def _write_key(path: str, key: SigningKey) -> None:
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists; refusing to overwrite a site key")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.to_hex() + "\n")


def cmd_keygen(a: argparse.Namespace) -> int:
    key = SigningKey.generate()
    _write_key(a.out, key)
    print(f"Wrote {a.out}. Add this site's public key to the hub config:\n")
    print(f'key = "{key.public_key().to_b64()}"')
    return 0


def cmd_pubkey(a: argparse.Namespace) -> int:
    print(SigningKey.load(a.key).public_key().to_b64())
    return 0


def _run(coro_factory: Callable[[], Awaitable[None]], stopper: Callable[[], None]) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stopper)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        await coro_factory()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def cmd_agent(a: argparse.Namespace) -> int:
    from .agent.runtime import Agent
    from .agent.sensors.factory import build_sensors

    cfg = load_agent_config(a.config)
    agent = Agent(cfg, SigningKey.load(cfg.key_path), build_sensors(cfg.sensors))
    _run(agent.run, agent.stop)
    return 0


def cmd_hub(a: argparse.Namespace) -> int:
    from .hub.server import Hub

    hub = Hub(load_hub_config(a.config))
    _run(hub.run, hub.stop)
    return 0


def _is_agent_config(path: str) -> bool:
    """An agent config has a [site] table; a hub config doesn't."""
    import tomllib

    try:
        with open(path, "rb") as f:
            return "site" in tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False  # let the loader report it properly


def cmd_check(a: argparse.Namespace) -> int:
    for path in a.configs:
        try:
            if _is_agent_config(path):
                from .agent.sensors.factory import build_sensors

                ag = load_agent_config(path)
                build_sensors(ag.sensors)  # validates sensor entries without touching hardware
                print(
                    f"{path}: agent config OK (site {ag.site}, {len(ag.sensors)} sensors, "
                    f"AMI {'on' if ag.ami else 'off'}, AREDN {'on' if ag.aredn else 'off'})"
                )
            else:
                from .hub.server import load_keys

                h = load_hub_config(path)
                load_keys(h)
                print(
                    f"{path}: hub config OK ({len(h.sites)} sites, {len(h.rules)} rules, "
                    f"{len(h.channels)} channels)"
                )
        except ConfigError as e:
            print(f"{path}: {e}", file=sys.stderr)
            return 1
    return 0


async def _send_command(hub: str, cmd: Command, wait: float) -> tuple[str, str]:
    """Queue ``cmd`` on the hub; with ``wait``, poll until the site reports back."""
    import aiohttp

    base = hub.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base}/api/commands", json=cmd.to_dict()) as r:
            if r.status != 202:
                return "refused by hub", (await r.text())[:300]
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            async with session.get(f"{base}/api/commands/{cmd.nonce}") as r:
                st = await r.json() if r.status == 200 else {}
            if st.get("status") in ("done", "refused", "expired"):
                return str(st["status"]), str(st.get("detail", ""))
    return "queued", "no answer from the site yet; it collects commands when it next syncs"


def cmd_command(a: argparse.Namespace) -> int:
    args: dict[str, object] = {"relay": a.relay, "action": a.action}
    if a.seconds is not None:
        args["seconds"] = a.seconds
    cmd = Command.create(a.site, "relay", args).signed(SigningKey.load(a.key))
    try:
        status, detail = asyncio.run(_send_command(a.hub, cmd, a.wait))
    except OSError as e:
        print(f"cannot reach {a.hub}: {e}", file=sys.stderr)
        return 1
    print(f"{status}: {detail}" if detail else status)
    return 0 if status in ("done", "queued") else 1


def cmd_summary(a: argparse.Namespace) -> int:
    from .hub.db import HubDB
    from .hub.summary import weekly_summary

    cfg = load_hub_config(a.config)
    db = HubDB(cfg.db_path)
    for group in cfg.groups:
        print(weekly_summary(cfg, db, group, time.time()))
        print()
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    """A hub and three simulated sites on this machine, for a look around."""
    from .agent.runtime import Agent
    from .agent.sensors import sim
    from .agent.sensors.base import Sensor
    from .hub.server import Hub

    tmp = tempfile.mkdtemp(prefix="sitemon-demo-")
    hub_url = f"http://127.0.0.1:{a.port}"
    specs = [
        ("shack", "Shack", 13.4, 0.0, False),
        ("hilltop", "Hilltop", 12.6, 0.0, True),  # door open
        ("ridge", "North Ridge", 12.05, 0.0, False),  # battery low
    ]
    keys = {sid: SigningKey.generate() for sid, *_ in specs}
    hub_cfg = HubConfig(
        listen=f"127.0.0.1:{a.port}",
        db_path=os.path.join(tmp, "hub.db"),
        public_url=hub_url,
        sites=[
            SiteConfig(id=sid, name=name, group="demo", keys=[keys[sid].public_key().to_b64()])
            for sid, name, *_ in specs
        ],
        rules=[
            RuleConfig(
                name="Battery low",
                type="threshold",
                metric="batt.v",
                sites=["*"],
                below=12.2,
                critical_below=11.8,
                hysteresis=0.1,
                unit="V",
            ),
            RuleConfig(name="Door open", type="state", metric="door.open", sites=["*"], bad=True),
            RuleConfig(
                name="Shack hot",
                type="threshold",
                metric="env.temp_c",
                sites=["*"],
                above=40,
                critical_above=50,
                unit="°C",
            ),
        ],
        channels=[]
        if not a.ntfy_topic
        else [
            ChannelConfig(
                type="ntfy",
                name="ntfy",
                min_severity="warn",
                url="https://ntfy.sh",
                topic=a.ntfy_topic,
            )
        ],
        groups={"demo": "Demo sites"},
        silence_warn=60,
        silence_critical=120,
        summary_enabled=False,
    )
    hub = Hub(hub_cfg)
    agents = []
    for sid, _name, volts, drift, door in specs:
        cfg = AgentConfig(
            site=sid,
            key_path="",
            data_dir=os.path.join(tmp, sid),
            hubs=[hub_url],
            sample_interval=5,
            heartbeat_interval=10,
            flush_interval=5,
            sync_interval=5,
            local_ui="",
        )
        sensors: list[Sensor] = [
            sim.battery("batt", float_v=volts, drift_v_per_day=drift, seed=hash(sid) & 0xFFFF),
            sim.environment("env", base_c=24.0),
            sim.switch("door", "open", active=door),
        ]
        if sid == "hilltop":
            sensors.append(sim.rf("rf", forward_w=50.0, swr=1.4))
        agents.append(Agent(cfg, keys[sid], sensors))

    async def main() -> None:
        tasks = [asyncio.create_task(hub.run())]
        await asyncio.sleep(0.5)
        # The third site stops after a minute, to show the silence alarm.
        tasks += [asyncio.create_task(ag.run()) for ag in agents]
        print(f"Dashboard: {hub_url}  (data in {tmp}; Ctrl+C to stop)")
        await asyncio.sleep(60)
        agents[-1].stop()
        await asyncio.gather(*tasks)

    def stop() -> None:
        for ag in agents:
            ag.stop()
        hub.stop()

    _run(main, stop)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sitemon", description=__doc__)
    p.add_argument("--version", action="version", version=f"sitemon {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("keygen", help="create a site signing key and print its public key")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_keygen)
    s = sub.add_parser("pubkey", help="print the public key for a site key file")
    s.add_argument("--key", required=True)
    s.set_defaults(fn=cmd_pubkey)
    s = sub.add_parser("agent", help="run the site agent")
    s.add_argument("--config", default="/etc/sitemon/agent.toml")
    s.set_defaults(fn=cmd_agent)
    s = sub.add_parser("hub", help="run the hub")
    s.add_argument("--config", default="/etc/sitemon/hub.toml")
    s.set_defaults(fn=cmd_hub)
    s = sub.add_parser("check", help="validate agent or hub config files")
    s.add_argument("configs", nargs="+")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("summary", help="print the weekly summary now")
    s.add_argument("--config", default="/etc/sitemon/hub.toml")
    s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("command", help="send a signed relay command to a site through a hub")
    s.add_argument("site")
    s.add_argument("relay")
    s.add_argument("action", choices=["pulse", "on", "off"])
    s.add_argument("--seconds", type=float, help="shorter than the relay's own limit")
    s.add_argument("--hub", required=True, help="a hub the site takes commands from")
    s.add_argument("--key", required=True, help="your operator key (sitemon keygen)")
    s.add_argument("--wait", type=float, default=90, help="seconds to wait for the result")
    s.set_defaults(fn=cmd_command)
    s = sub.add_parser("demo", help="run a hub with three simulated sites on this machine")
    s.add_argument("--port", type=int, default=8470)
    s.add_argument("--ntfy-topic", help="also push demo alerts to this ntfy.sh topic")
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        rc: int = a.fn(a)
        return rc
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
