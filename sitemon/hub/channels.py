"""Alert channels: Discord webhook, ntfy push and email. Each raises
ChannelError on failure and the outbox decides when to retry."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

import aiohttp

from ..config import ChannelConfig

NTFY_PRIORITY = {"info": "2", "warn": "4", "critical": "5"}
NTFY_TAGS = {"info": "white_check_mark", "warn": "warning", "critical": "rotating_light"}


class ChannelError(Exception):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _ascii(s: str) -> str:
    # HTTP header values must be latin-1; ntfy reads the title from a header.
    return s.encode("ascii", "replace").decode("ascii")


async def send_discord(
    session: aiohttp.ClientSession, ch: ChannelConfig, title: str, body: str
) -> None:
    content = f"**{title}**\n{body}"
    if len(content) > 2000:  # Discord's limit
        content = content[:1997] + "..."
    # Alert text must never be able to @everyone the whole server.
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    async with session.post(ch.url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status >= 300:
            raise ChannelError(f"Discord answered HTTP {r.status}")


async def send_ntfy(
    session: aiohttp.ClientSession, ch: ChannelConfig, title: str, body: str, severity: str
) -> None:
    headers = {
        "Title": _ascii(title),
        "Priority": NTFY_PRIORITY.get(severity, "3"),
        "Tags": NTFY_TAGS.get(severity, ""),
    }
    if ch.token:
        headers["Authorization"] = f"Bearer {ch.token}"
    async with session.post(
        f"{ch.url}/{ch.topic}",
        data=body.encode(),
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as r:
        if r.status >= 300:
            raise ChannelError(f"ntfy answered HTTP {r.status}")


def _send_email_blocking(ch: ChannelConfig, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"[sitemon] {title}"
    msg["From"] = ch.sender
    msg["To"] = ", ".join(ch.to)
    msg.set_content(body)
    try:
        with smtplib.SMTP(ch.smtp_host, ch.smtp_port, timeout=30) as s:
            if ch.starttls:
                s.starttls(context=ssl.create_default_context())
            if ch.smtp_user:
                s.login(ch.smtp_user, ch.smtp_password)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise ChannelError(f"email: {type(e).__name__}: {e}") from e


async def send(
    session: aiohttp.ClientSession, ch: ChannelConfig, title: str, body: str, severity: str
) -> None:
    try:
        if ch.type == "discord":
            await send_discord(session, ch, title, body)
        elif ch.type == "ntfy":
            await send_ntfy(session, ch, title, body, severity)
        else:
            await asyncio.to_thread(_send_email_blocking, ch, title, body)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ChannelError(f"{ch.type}: {type(e).__name__}: {e}") from e
    # A header or address the library refuses, e.g. a newline in a name: no retry helps.
    except ValueError as e:
        raise ChannelError(f"{ch.type}: {type(e).__name__}: {e}", permanent=True) from e
