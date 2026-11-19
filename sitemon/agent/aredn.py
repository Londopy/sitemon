"""AREDN node state from the local node's sysinfo JSON. Fields move between
firmware releases, so missing ones are left out."""

from __future__ import annotations

from typing import Any

import aiohttp


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"reachable": True}
    if isinstance(info.get("node"), str):
        out["node"] = info["node"]
    details = _obj(info.get("node_details"))
    for src, dst in (("firmware_version", "firmware"), ("model", "model")):
        if isinstance(details.get(src), str):
            out[dst] = details[src]
    sysinfo = _obj(info.get("sysinfo"))
    up = sysinfo.get("uptime")
    if isinstance(up, str):
        out["uptime"] = up
    loads = sysinfo.get("loads")
    if isinstance(loads, list) and loads and isinstance(loads[0], (int, float)):
        out["load1"] = float(loads[0])
    links = info.get("link_info")
    if isinstance(links, dict):
        out["link_count"] = len(links)
        # RF links are the ones most likely to fade at a mountain site.
        out["rf_links"] = sum(
            1 for v in links.values() if isinstance(v, dict) and v.get("linkType") == "RF"
        )
    meshrf = _obj(info.get("meshrf"))
    if isinstance(meshrf.get("freq"), (str, int, float)):
        out["rf_freq"] = str(meshrf["freq"])
    return out


async def fetch(session: aiohttp.ClientSession, url: str, timeout: float = 10.0) -> dict[str, Any]:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        r.raise_for_status()
        data = await r.json(content_type=None)
    if not isinstance(data, dict):
        raise ValueError("sysinfo was not a JSON object")
    return summarize(data)
