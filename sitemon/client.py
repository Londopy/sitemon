"""Talking to a hub or a site's local status page over HTTP, for the CLI and the TUI."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from ._commands import Command

TIMEOUT = aiohttp.ClientTimeout(total=15)


class ApiError(Exception):
    pass


async def get_json(session: aiohttp.ClientSession, url: str) -> Any:
    try:
        async with session.get(url, timeout=TIMEOUT) as r:
            if r.status != 200:
                raise ApiError(f"{url} answered HTTP {r.status}: {(await r.text())[:200]}")
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise ApiError(f"can't read {url}: {e or type(e).__name__}") from e


async def send_command(
    session: aiohttp.ClientSession, hub: str, cmd: Command, wait: float, poll: float = 3.0
) -> tuple[str, str]:
    """Queue ``cmd`` on the hub; with ``wait``, poll until the site reports back."""
    base = hub.rstrip("/")
    async with session.post(f"{base}/api/commands", json=cmd.to_dict(), timeout=TIMEOUT) as r:
        if r.status != 202:
            return "refused by hub", (await r.text())[:300]
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        try:
            async with session.get(f"{base}/api/commands/{cmd.nonce}", timeout=TIMEOUT) as r:
                st = await r.json() if r.status == 200 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        if isinstance(st, dict) and st.get("status") in ("done", "refused", "expired"):
            return str(st["status"]), str(st.get("detail", ""))
    return "queued", "no answer from the site yet; it collects commands when it next syncs"
