"""A read-only AMI client for app_rpt, loopback only.

It sends only Login, RptStatus, CoreStatus and (for the optional registration
check) Command with one of ALLOWED_COMMANDS, and refuses anything else itself.
The AMI user needs ``read = system,call`` and ``write = reporting``; the
registration check also needs ``write = command``, which allows any CLI command.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

Message = list[tuple[str, str]]

ALLOWED_COMMANDS = frozenset({"rpt show registrations", "iax2 show registry"})
# Spoken alerts play a file the agent wrote itself; nothing else is played.
_LOCALPLAY = re.compile(r"rpt localplay \d+ /[A-Za-z0-9_./-]+")
_ACTIONS = frozenset({"login", "rptstatus", "corestatus", "command"})


class AmiError(Exception):
    pass


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _get(msg: Message, key: str) -> str | None:
    k = key.lower()
    for mk, v in msg:
        if mk.lower() == k:
            return v
    return None


def _all(msg: Message, key: str) -> list[str]:
    k = key.lower()
    return [v for mk, v in msg if mk.lower() == k]


def _refuse_unsafe(name: str, fields: dict[str, str], sound_dir: str | None) -> None:
    # Defence in depth on top of the AMI user's permissions.
    if any(c in s for s in (name, *fields, *fields.values()) for c in "\r\n"):
        # A line break would end the header early and smuggle in another one.
        raise AmiError(f"refusing to send AMI action {name!r}: line break in a field")
    if name.lower() not in _ACTIONS:
        raise AmiError(f"refusing to send AMI action {name!r}: sitemon only reads")
    if name.lower() == "command" and (
        list(fields) != ["Command"] or not _allowed_command(fields["Command"], sound_dir)
    ):
        raise AmiError(
            f"refusing to send CLI command {fields.get('Command')!r}: only "
            + " and ".join(repr(c) for c in sorted(ALLOWED_COMMANDS))
            + " are allowed"
        )


def _allowed_command(cmd: str, sound_dir: str | None) -> bool:
    if cmd in ALLOWED_COMMANDS:
        return True
    # Only files in the announcement folder, which only the agent writes to.
    return (
        sound_dir is not None
        and _LOCALPLAY.fullmatch(cmd) is not None
        and ".." not in cmd
        and cmd.split(" ", 3)[3].startswith(sound_dir.rstrip("/") + "/")
    )


@dataclass
class NodeState:
    keyed_tx: bool
    keyed_rx: bool
    links: list[str]
    registered: bool | None
    uptime_s: int | None

    def as_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "connected": True,
            "keyed_tx": self.keyed_tx,
            "keyed_rx": self.keyed_rx,
            "links": self.links,
            "link_count": len(self.links),
        }
        # Unknowns are left out, so the hub's "not registered" rule can't fire on them.
        if self.registered is not None:
            data["registered"] = self.registered
        if self.uptime_s is not None:
            data["uptime_s"] = self.uptime_s
        return data


def parse_xstat(msg: Message) -> tuple[bool, bool, list[str]]:
    """Keyed state and linked nodes from an ``RptStatus``/``XStat`` response."""
    vars_: dict[str, str] = {}
    for v in _all(msg, "Var"):
        name, _, val = v.partition("=")
        vars_[name.strip()] = val.strip()
    links: list[str] = []
    for line in _all(msg, "LinkedNodes"):
        # "T2000, R2001, L52769": the letter is the link mode, the rest the node.
        for tok in line.split(","):
            tok = tok.strip()
            if tok:
                links.append(tok[1:] if tok[0].isalpha() and tok[1:].isdigit() else tok)
    return vars_.get("RPT_TXKEYED") == "1", vars_.get("RPT_RXKEYED") == "1", links


def parse_corestatus(msg: Message, now: float | None = None) -> int:
    """``CoreStatus`` response -> seconds since Asterisk started.

    Asterisk gives its start as a local date and time. It runs on this
    machine (AMI is loopback only), so that is our local time too.
    """
    if (_get(msg, "Response") or "").lower() != "success":
        raise AmiError(f"CoreStatus failed: {_get(msg, 'Message') or 'no reason given'}")
    try:
        started = datetime.strptime(
            f"{_get(msg, 'CoreStartupDate')} {_get(msg, 'CoreStartupTime')}", "%Y-%m-%d %H:%M:%S"
        )
        started_at = started.timestamp()
    except (ValueError, OverflowError, OSError):
        raise AmiError("could not read Asterisk's start time from CoreStatus") from None
    # NTP may have stepped the clock back since.
    return max(0, int((time.time() if now is None else now) - started_at))


def parse_registered(text: str) -> bool:
    # Case-sensitive on purpose: "Unregistered" must not count as registered.
    return re.search(r"\bRegistered\b", text) is not None


class AmiClient:
    """AMI over loopback, read-only. See the module docstring for the AMI user
    this needs: ``write = reporting``, plus ``write = command`` only for the
    optional registration check."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        secret: str,
        *,
        timeout: float = 10.0,
        sound_dir: str | None = None,
    ):
        if not _is_loopback(host):
            raise AmiError(f"refusing to connect to AMI on {host}: loopback only")
        self.host, self.port, self.user = host, port, user
        self._secret = secret
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._connecting = asyncio.Lock()
        self.sound_dir = sound_dir

    @property
    def connected(self) -> bool:
        # asyncio doesn't notice the peer closing until the session is used.
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        # The AMI poll and an announcement can both find the session down.
        async with self._connecting:
            if not self.connected:
                await self._connect()

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        assert self._reader is not None
        banner = await asyncio.wait_for(self._reader.readline(), self.timeout)
        if b"Call Manager" not in banner:
            await self.close()
            raise AmiError(f"not an AMI server: {banner[:60]!r}")
        resp = await self.action("Login", Username=self.user, Secret=self._secret, Events="off")
        if (_get(resp, "Response") or "").lower() != "success":
            await self.close()
            raise AmiError(f"AMI login failed: {_get(resp, 'Message') or 'no reason given'}")

    async def close(self) -> None:
        w, self._writer, self._reader = self._writer, None, None
        if w is not None:
            w.close()
            try:
                await w.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _read_message(self) -> tuple[Message, str]:
        """One AMI message (key/value lines up to a blank line), plus raw
        output for the old ``Response: Follows`` command format."""
        assert self._reader is not None
        msg: Message = []
        raw: list[str] = []
        follows = False
        while True:
            line_b = await asyncio.wait_for(self._reader.readline(), self.timeout)
            if not line_b:
                raise AmiError("AMI connection closed")
            line = line_b.decode("utf-8", "replace").rstrip("\r\n")
            if follows:
                if line.strip() == "--END COMMAND--":
                    follows = False
                    continue
                if ":" in line and not raw and line.split(":", 1)[0] in ("ActionID", "Privilege"):
                    k, _, v = line.partition(":")
                    msg.append((k.strip(), v.strip()))
                else:
                    raw.append(line)
                continue
            if line == "":
                if msg:
                    return msg, "\n".join(raw)
                continue
            k, sep, v = line.partition(":")
            if not sep:
                raw.append(line)
                continue
            msg.append((k.strip(), v.strip()))
            if k.strip().lower() == "response" and v.strip().lower() == "follows":
                follows = True

    async def action(self, name: str, **fields: str) -> Message:
        msg, _ = await self._action(name, **fields)
        return msg

    async def _action(self, name: str, **fields: str) -> tuple[Message, str]:
        _refuse_unsafe(name, fields, self.sound_dir)  # every request goes through here
        if self._writer is None:
            raise AmiError("not connected")
        async with self._lock:
            aid = f"sitemon-{next(self._ids)}"
            lines = [f"Action: {name}", f"ActionID: {aid}"]
            lines += [f"{k}: {v}" for k, v in fields.items()]
            self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await self._writer.drain()
            while True:
                msg, raw = await self._read_message()
                if _get(msg, "ActionID") == aid:
                    return msg, raw
                # Some app_rpt builds send events even with events off.

    async def play(self, node: int, path: str) -> None:
        """Play a sound file (path without its extension) on the node's transmitter."""
        await self.command(f"rpt localplay {int(node)} {path}")

    async def command(self, cmd: str) -> str:
        """Run one of ALLOWED_COMMANDS; returns its text output (both AMI
        formats). Any other command raises AmiError without being sent."""
        msg, raw = await self._action("Command", Command=cmd)
        resp = (_get(msg, "Response") or "").lower()
        if resp == "error":
            raise AmiError(f"command {cmd!r} failed: {_get(msg, 'Message')}")
        out = _all(msg, "Output")
        return "\n".join(out) if out else raw

    async def node_state(self, node: int, registration_command: str = "") -> NodeState:
        """Keyed state, links and uptime; registration only when
        ``registration_command`` is set (it needs ``write = command``)."""
        xs = await self.action("RptStatus", COMMAND="XStat", NODE=str(node))
        if (_get(xs, "Response") or "").lower() != "success":
            raise AmiError(f"XStat for node {node} failed: {_get(xs, 'Message')}")
        tx, rx, links = parse_xstat(xs)
        cs = await self.action("CoreStatus")
        try:
            uptime: int | None = parse_corestatus(cs)
        except AmiError:
            # Usually no write = reporting; uptime is optional, the node isn't down.
            uptime = None
        registered: bool | None = None
        if registration_command:
            try:
                registered = parse_registered(await self.command(registration_command))
            except AmiError as e:
                # A lost session means the poll failed; the caller reconnects.
                if "closed" in str(e) or "not connected" in str(e):
                    raise
                # Otherwise we just don't know, which isn't "not registered".
                registered = None
        return NodeState(
            keyed_tx=tx, keyed_rx=rx, links=links, registered=registered, uptime_s=uptime
        )
