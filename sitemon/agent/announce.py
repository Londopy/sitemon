"""Spoken alerts on the repeater: text to speech, converted for Asterisk,
played with ``rpt localplay``. Off unless enabled; see deploy/README.md."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable

from ..config import AnnounceConfig
from .._commands import speakable
from .ami import AmiClient, AmiError

STEP_TIMEOUT = 30.0
KEEP_FILES = 86400.0


class AnnounceError(Exception):
    pass


class Announcer:
    def __init__(
        self,
        cfg: AnnounceConfig,
        ami: AmiClient,
        node: int,
        directory: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.ami = ami
        self.node = node
        self.directory = os.path.abspath(directory)
        self.clock = clock
        self.last: float | None = None
        self._lock = asyncio.Lock()

    async def say(self, text: str, nonce: str) -> str:
        text = speakable(text)
        if not text:
            raise AnnounceError("nothing to say")
        # One at a time: two hubs' commands must not both get past min_gap.
        async with self._lock:
            return await self._say(text, nonce)

    async def _say(self, text: str, nonce: str) -> str:
        if self.last is not None and self.clock() - self.last < self.cfg.min_gap:
            raise AnnounceError("another announcement played less than min_gap ago")
        os.makedirs(self.directory, exist_ok=True)
        self._clean()
        base = os.path.join(self.directory, nonce)
        names = {"text": text, "wav": base + ".tts.wav", "out": base + ".wav"}
        await self._run(self.cfg.say_command, names)
        await self._run(self.cfg.convert_command, names)
        try:
            if not self.ami.connected:
                await self.ami.connect()
            await self.ami.play(self.node, base)
        except (AmiError, OSError, asyncio.TimeoutError) as e:
            raise AnnounceError(f"playing it failed: {e or type(e).__name__}") from e
        self.last = self.clock()
        return f"announced: {text}"

    async def _run(self, template: list[str], names: dict[str, str]) -> None:
        # Arguments are passed as a list, never through a shell, so the text
        # can't inject anything.
        args = [a.format(**names) for a in template]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as e:
            raise AnnounceError(f"cannot run {args[0]}: {e}") from e
        try:
            _, err = await asyncio.wait_for(proc.communicate(), STEP_TIMEOUT)
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise AnnounceError(f"{args[0]} took longer than {STEP_TIMEOUT:g} s") from e
        if proc.returncode != 0:
            raise AnnounceError(f"{args[0]} failed: {err.decode(errors='replace').strip()[:200]}")

    def _clean(self) -> None:
        cutoff = time.time() - KEEP_FILES
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            if name.endswith(".wav") and os.path.getmtime(path) < cutoff:
                os.remove(path)
