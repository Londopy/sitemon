from __future__ import annotations

import dataclasses
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
