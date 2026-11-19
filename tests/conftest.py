from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sitemon import SigningKey
from sitemon.config import HubConfig, RuleConfig, SiteConfig
from sitemon.hub.db import HubDB


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()


def make_hub_config(sites: dict[str, SigningKey], **kw: Any) -> HubConfig:
    cfg = HubConfig(
        listen="127.0.0.1:0",
        db_path=":memory:",
        public_url="https://hub.example",
        sites=[
            SiteConfig(id=s, name=s.upper(), group="group", keys=[k.public_key().to_b64()])
            for s, k in sites.items()
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
        ],
        channels=[],
        groups={"group": "Test Group"},
        summary_enabled=False,
    )
    return dataclasses.replace(cfg, **kw)


@pytest.fixture
def hubdb() -> HubDB:
    return HubDB(":memory:")


class FakeAmi:
    """Just enough of Asterisk's manager interface for the agent's reads."""

    def __init__(
        self,
        *,
        secret: str = "s3cret",
        links: str = "T2000, R2001",
        txkeyed: str = "0",
        registered: bool = True,
        old_format: bool = False,
        uptime: int = 98765,
    ) -> None:
        self.secret = secret
        self.links = links
        self.txkeyed = txkeyed
        self.registered = registered
        self.old_format = old_format
        self.uptime = uptime
        #: Actions (lower case) answered "Permission denied", as for an AMI
        #: user without the write class the action needs.
        self.denied: set[str] = set()
        #: False: connections are dropped at once, like Asterisk going down
        #: (without the 2 s a refused loopback connect costs on Windows).
        self.answering = True
        self.actions: list[dict[str, str]] = []
        self.clients: list[asyncio.StreamWriter] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            # Since 3.12, wait_closed() also waits for open connections, so
            # drop them first (as a dying Asterisk would).
            self.drop_sessions()
            await self.server.wait_closed()

    def drop_sessions(self) -> None:
        """Close every client session from this end, as an Asterisk restart does."""
        for w in self.clients:
            w.close()
        self.clients.clear()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        if not self.answering:
            w.close()
            return
        self.clients.append(w)
        w.write(b"Asterisk Call Manager/7.0.3\r\n")
        authed = False
        try:
            while True:
                msg: dict[str, str] = {}
                while True:
                    line = await r.readline()
                    if not line:
                        return
                    s = line.decode().rstrip("\r\n")
                    if s == "":
                        break
                    k, _, v = s.partition(":")
                    msg[k.strip()] = v.strip()
                if not msg:
                    continue
                self.actions.append(msg)
                aid = msg.get("ActionID", "")
                act = msg.get("Action", "").lower()
                if act == "login":
                    authed = msg.get("Secret") == self.secret
                    resp = "Success" if authed else "Error"
                    w.write(f"Response: {resp}\r\nActionID: {aid}\r\nMessage: x\r\n\r\n".encode())
                elif not authed:
                    w.write(f"Response: Error\r\nActionID: {aid}\r\nMessage: auth\r\n\r\n".encode())
                elif act in self.denied:
                    w.write(
                        f"Response: Error\r\nActionID: {aid}\r\n"
                        "Message: Permission denied\r\n\r\n".encode()
                    )
                elif act == "rptstatus":
                    # A stray event first: the client must skip it.
                    w.write(b"Event: RPT_ALINKS\r\nNode: 1999\r\n\r\n")
                    w.write(
                        (
                            f"Response: Success\r\nActionID: {aid}\r\nNode: 1999\r\n"
                            f"LinkedNodes: {self.links}\r\nVar: RPT_TXKEYED={self.txkeyed}\r\n"
                            "Var: RPT_RXKEYED=0\r\n\r\n"
                        ).encode()
                    )
                elif act == "corestatus":
                    import time
                    from datetime import datetime

                    # Asterisk's local time, like the real thing.
                    t = datetime.fromtimestamp(time.time() - self.uptime)
                    w.write(
                        (
                            f"Response: Success\r\nActionID: {aid}\r\n"
                            f"CoreStartupDate: {t:%Y-%m-%d}\r\nCoreStartupTime: {t:%H:%M:%S}\r\n"
                            f"CoreReloadDate: {t:%Y-%m-%d}\r\nCoreReloadTime: {t:%H:%M:%S}\r\n"
                            "CoreCurrentCalls: 0\r\nCoreProcessedCalls: 3\r\n\r\n"
                        ).encode()
                    )
                elif act == "command":
                    # The registration check is the only command sitemon sends.
                    out = [
                        "Host  Username  Perceived  Refresh  State",
                        "x  1999  1.2.3.4  60  "
                        + ("Registered" if self.registered else "Unregistered"),
                    ]
                    if self.old_format:
                        w.write(
                            (
                                f"Response: Follows\r\nPrivilege: Command\r\nActionID: {aid}\r\n"
                                + "\r\n".join(out)
                                + "\r\n--END COMMAND--\r\n\r\n"
                            ).encode()
                        )
                    else:
                        w.write(
                            (
                                f"Response: Success\r\nActionID: {aid}\r\n"
                                + "".join(f"Output: {o}\r\n" for o in out)
                                + "\r\n"
                            ).encode()
                        )
                else:
                    w.write(f"Response: Error\r\nActionID: {aid}\r\n\r\n".encode())
                await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            w.close()


@pytest.fixture
async def fake_ami() -> AsyncIterator[FakeAmi]:
    ami = FakeAmi()
    await ami.start()
    yield ami
    await ami.stop()
