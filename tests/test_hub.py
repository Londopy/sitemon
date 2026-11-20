from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sitemon import Event, SigningKey
from sitemon._events import canonical_json, envelope_body
from sitemon.config import ChannelConfig, RuleConfig
from sitemon.hub import alerts, rules
from sitemon.hub.db import HubDB
from sitemon.hub.ingest import IngestError, ingest
from sitemon.hub.server import Hub
from sitemon.hub.summary import low_trend, weekly_summary

from .conftest import make_hub_config

NOW = 1_800_000_000.0


class Site:
    """Builds signed uploads the way the agent does, with controllable clocks."""

    def __init__(self, sid: str = "s1", key: SigningKey | None = None, boot: str = "boot1") -> None:
        self.sid = sid
        self.key = key or SigningKey.generate()
        self.boot = boot
        self.seq = 0
        self.mono = 1_000 * 10**9  # agent monotonic clock, ns

    def event(
        self,
        kind: str,
        source: str,
        data: dict[str, Any],
        *,
        wall: float = NOW,
        advance: float = 30.0,
    ) -> Event:
        self.seq += 1
        self.mono += int(advance * 1e9)
        return Event.create(
            site=self.sid,
            seq=self.seq,
            boot=self.boot,
            kind=kind,
            source=source,
            data=data,
            mono_ns=self.mono,
            wall=wall,
        ).signed(self.key)

    def upload(self, events: list[Event], *, wall: float = NOW) -> dict[str, Any]:
        env: dict[str, Any] = {
            "site": self.sid,
            "boot": self.boot,
            "mono_ns": self.mono,
            "wall": wall,
            "events": [e.to_dict() for e in events],
        }
        env["sig"] = self.key.sign(canonical_json(envelope_body(env)))
        return env


def keys_for(*sites: Site) -> dict[str, Any]:
    return {s.sid: [s.key.public_key()] for s in sites}


def test_ingest_stores_points_and_latest(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload(
        [
            s.event("reading", "batt", {"v": 13.2, "a": 1.5}),
            s.event("state", "door", {"open": False}),
        ]
    )
    res = ingest(hubdb, keys_for(s), up, now=NOW)
    assert res.acked_through == 2 and len(res.stored) == 2
    assert hubdb.latest("s1")["batt.v"][0] == 13.2
    assert hubdb.latest("s1")["door.open"][0] is False
    assert hubdb.raw_points("s1", "door.open", 0, NOW + 1)[0][1] == 0.0


def test_retried_upload_is_idempotent(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload([s.event("reading", "batt", {"v": 13.2})])
    ingest(hubdb, keys_for(s), up, now=NOW)
    res = ingest(hubdb, keys_for(s), up, now=NOW + 5)
    assert res.stored == [] and res.acked_through == 1
    assert len(hubdb.raw_points("s1", "batt.v", 0, NOW + 10)) == 1


@pytest.mark.parametrize("tamper", ["envelope", "wrong_key"])
def test_bad_envelope_stores_nothing(hubdb: HubDB, tamper: str) -> None:
    s = Site()
    up = s.upload([s.event("reading", "batt", {"v": 13.2})])
    keys = keys_for(s)
    if tamper == "envelope":
        up["mono_ns"] += 1
    else:
        keys = {"s1": [SigningKey.generate().public_key()]}
    with pytest.raises(IngestError) as ei:
        ingest(hubdb, keys, up, now=NOW)
    assert ei.value.status == 403
    assert hubdb.latest("s1") == {} and hubdb.last_contact("s1") is None


@pytest.mark.parametrize("bad", ["tampered", "other_site", "nan", "old_key", "garbage"])
def test_bad_event_in_genuine_upload_is_set_aside(hubdb: HubDB, bad: str) -> None:
    # One bad event must not wedge the site's queue: the rest are stored and
    # the whole upload acknowledged.
    s = Site()
    good = s.event("reading", "batt", {"v": 13.2})
    if bad == "tampered":
        ev = s.event("reading", "env", {"temp_c": 20.0}).to_dict()
        ev["data"]["temp_c"] = 99.0
    elif bad == "other_site":
        ev = Site("s2").event("reading", "env", {"temp_c": 1.0}).to_dict()
        ev["seq"] = 2
    elif bad == "nan":
        ev = s.event("reading", "env", {"temp_c": float("nan")}).to_dict()
    elif bad == "old_key":
        ev = Site(key=SigningKey.generate()).event("reading", "env", {"temp_c": 1.0}).to_dict()
        ev["seq"] = 2
    else:
        ev = {"seq": 2, "nonsense": True}
    up = s.upload([good])
    up["events"].append(ev)
    up["sig"] = s.key.sign(canonical_json(envelope_body(up)))
    res = ingest(hubdb, keys_for(s), up, now=NOW)
    assert [e.seq for e, _ in res.stored] == [1]
    assert res.acked_through == 2 and len(res.quarantined) == 1
    assert "env.temp_c" not in hubdb.latest("s1")
    assert len(hubdb.rejected_since("s1", 0)) == 1


def test_unknown_site_and_garbage(hubdb: HubDB) -> None:
    with pytest.raises(IngestError) as ei:
        ingest(hubdb, {}, Site().upload([]), now=NOW)
    assert ei.value.status == 403
    with pytest.raises(IngestError) as ei:
        ingest(hubdb, {}, {"site": "x"}, now=NOW)
    assert ei.value.status == 400


def test_key_rotation_accepts_either_key(hubdb: HubDB) -> None:
    s = Site()
    old = SigningKey.generate().public_key()
    ingest(
        hubdb,
        {"s1": [old, s.key.public_key()]},
        s.upload([s.event("reading", "batt", {"v": 13.0})]),
        now=NOW,
    )


def test_counter_reset_is_stored_and_reported(hubdb: HubDB) -> None:
    # The SD card was replaced: same key, new boot, counter back near zero.
    s = Site(boot="old")
    s.seq = 1000
    ingest(hubdb, keys_for(s), s.upload([s.event("reading", "batt", {"v": 13.0})]), now=NOW)
    fresh = Site(key=s.key, boot="new")
    fresh.seq = 0
    res = ingest(
        hubdb,
        keys_for(fresh),
        fresh.upload([fresh.event("reading", "batt", {"v": 9.0})]),
        now=NOW + 60,
    )
    assert res.counter_reset and len(res.stored) == 1
    assert hubdb.latest("s1")["batt.v"][0] == 9.0


def test_replayed_upload_does_not_count_as_contact(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload([s.event("heartbeat", "agent", {"queue": 0})])
    ingest(hubdb, keys_for(s), up, now=NOW)
    ingest(hubdb, keys_for(s), up, now=NOW + 7200)  # captured and replayed later
    assert hubdb.last_contact("s1") == NOW


def test_events_placed_by_monotonic_clock_not_wall(hubdb: HubDB) -> None:
    # The Pi booted with no RTC and no NTP: its wall clock says 1970.
    s = Site()
    evs = [s.event("reading", "batt", {"v": 13.0 + i / 10}, wall=12.0) for i in range(3)]
    ingest(hubdb, keys_for(s), s.upload(evs, wall=12.0), now=NOW)
    pts = hubdb.raw_points("s1", "batt.v", 0, NOW + 1)
    # Uploaded right after the last reading: 60 s, 30 s and 0 s before receipt.
    assert [round(NOW - t) for t, _ in pts] == [60, 30, 0]


def test_backlog_does_not_overwrite_newer_latest(hubdb: HubDB) -> None:
    s_old = Site(boot="old")
    old_ev = s_old.event("reading", "batt", {"v": 11.0}, wall=NOW - 3600)
    s_new = Site(key=s_old.key, boot="new")
    s_new.seq = 100
    new_ev = s_new.event("reading", "batt", {"v": 13.0})
    ingest(hubdb, keys_for(s_new), s_new.upload([new_ev]), now=NOW)
    # The old boot's events arrive later, in an upload from the new process.
    up = s_new.upload([old_ev])
    ingest(hubdb, keys_for(s_new), up, now=NOW + 10)
    assert hubdb.latest("s1")["batt.v"][0] == 13.0
    assert len(hubdb.raw_points("s1", "batt.v", NOW - 4000, NOW + 20)) == 2


def hub_with(site: Site, **kw: Any) -> Hub:
    return Hub(make_hub_config({site.sid: site.key}, **kw), db=HubDB(":memory:"))


def feed(hub: Hub, s: Site, kind: str, source: str, data: dict[str, Any], now: float) -> None:
    ingest(hub.db, hub.keys, s.upload([s.event(kind, source, data)]), now=now)


def feed_at(hub: Hub, s: Site, source: str, data: dict[str, Any], t: float) -> None:
    """A reading at ``t``, with the site's monotonic clock kept in step, so
    readings days apart land days apart."""
    s.mono = int((t - 30) * 1e9)
    feed(hub, s, "reading", source, data, t)


def test_threshold_open_escalate_resolve_with_hysteresis() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "reading", "batt", {"v": 12.1}, NOW)
    n = hub.evaluate("s1", NOW)
    assert [x.title for x in n] == ["WARN: S1"] and "12.1 V" in n[0].body
    assert hub.evaluate("s1", NOW + 30) == []  # de-duplicated
    feed(hub, s, "reading", "batt", {"v": 11.7}, NOW + 60)
    assert [x.title for x in hub.evaluate("s1", NOW + 60)] == ["ESCALATED to CRITICAL: S1"]
    feed(hub, s, "reading", "batt", {"v": 12.25}, NOW + 90)  # inside the hysteresis band
    assert hub.evaluate("s1", NOW + 90) == []
    assert hub.db.open_alert("s1", "rule:Battery low") is not None
    feed(hub, s, "reading", "batt", {"v": 12.4}, NOW + 120)
    out = hub.evaluate("s1", NOW + 120)
    assert [x.title for x in out] == ["Resolved: S1"] and out[0].severity == "critical"


def test_hysteresis_never_opens_an_alert() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "reading", "batt", {"v": 12.25}, NOW)
    assert hub.evaluate("s1", NOW) == []


def test_hysteresis_does_not_lower_the_critical_level() -> None:
    # warn below 12.2, critical below 11.8, hysteresis 0.1
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "reading", "batt", {"v": 12.0}, NOW)
    hub.evaluate("s1", NOW)
    for i, v in enumerate([11.85, 11.89, 11.81, 11.85]):
        feed(hub, s, "reading", "batt", {"v": v}, NOW + 30 * (i + 1))
        assert hub.evaluate("s1", NOW + 30 * (i + 1)) == []  # still just a warning
    feed(hub, s, "reading", "batt", {"v": 11.7}, NOW + 200)
    assert hub.evaluate("s1", NOW + 200)[0].severity == "critical"
    feed(hub, s, "reading", "batt", {"v": 11.85}, NOW + 230)  # held critical by hysteresis
    assert hub.evaluate("s1", NOW + 230) == []
    a = hub.db.open_alert("s1", "rule:Battery low")
    assert a is not None and a.severity == "critical"


def test_state_rule_and_sensor_fault() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "state", "door", {"open": True}, NOW)
    feed(hub, s, "fault", "env", {"ok": False, "error": "I2C timeout at 0x76"}, NOW)
    titles = {x.body.split("\n")[0] for x in hub.evaluate("s1", NOW)}
    assert titles == {"Door open", "sensor env is failing: I2C timeout at 0x76"}
    feed(hub, s, "fault", "env", {"ok": True}, NOW + 30)
    feed(hub, s, "state", "door", {"open": False}, NOW + 30)
    assert len(hub.evaluate("s1", NOW + 30)) == 2  # both resolved


def test_ami_down_is_critical() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "state", "ami", {"connected": False, "error": "connection refused"}, NOW)
    (n,) = hub.evaluate("s1", NOW)
    assert n.severity == "critical" and "connection refused" in n.body


def test_silence_alarm_from_hub_clock() -> None:
    s = Site()
    hub = hub_with(s, silence_warn=300, silence_critical=900)
    feed(hub, s, "heartbeat", "agent", {"queue": 0}, NOW)
    assert hub.evaluate("s1", NOW + 200) == []
    assert hub.evaluate("s1", NOW + 301)[0].title == "WARN: S1"
    esc = hub.evaluate("s1", NOW + 901)[0]
    assert esc.severity == "critical" and "no contact for 15 min" in esc.body
    feed(hub, s, "heartbeat", "agent", {"queue": 40}, NOW + 1000)
    assert hub.evaluate("s1", NOW + 1000)[0].title == "Resolved: S1"


def test_never_seen_site_goes_silent_after_it_is_added() -> None:
    s = Site()
    hub = hub_with(s, silence_warn=300, silence_critical=900)
    hub.first_configured["s1"] = hub.process_started = NOW
    assert hub.evaluate("s1", NOW + 200) == []
    (n,) = hub.evaluate("s1", NOW + 400)
    assert "never reported" in n.body and n.severity == "warn"


def test_hub_restart_gives_sites_a_grace_period() -> None:
    # The hub itself was down for two hours; the site was fine all along.
    s = Site()
    hub = hub_with(s, silence_warn=300, silence_critical=900)
    feed(hub, s, "heartbeat", "agent", {}, NOW - 7200)
    hub.first_configured["s1"] = NOW - 86400
    hub.process_started = NOW
    assert hub.evaluate("s1", NOW + 60) == []
    (n,) = hub.evaluate("s1", NOW + 301)  # still nothing after a full window: now it counts
    assert "no contact for 2 h" in n.body and n.severity == "warn"


def test_refused_uploads_annotate_silence_but_never_alert_alone() -> None:
    s = Site()
    hub = hub_with(s, silence_warn=300, silence_critical=900)
    hub.first_configured["s1"] = hub.process_started = NOW
    feed(hub, s, "heartbeat", "agent", {}, NOW)
    hub.bad_uploads["s1"] = "bad upload signature from 's1'"
    assert hub.evaluate("s1", NOW + 10) == []  # anyone could have sent it
    (n,) = hub.evaluate("s1", NOW + 400)
    assert "uploads are being refused: bad upload signature" in n.body


def test_quarantined_events_raise_a_warning() -> None:
    s = Site()
    hub = hub_with(s)
    hub.db.x(
        "INSERT INTO rejected (site, seq, recv, reason, raw) VALUES ('s1', 5, ?, 'x', '{}')", (NOW,)
    )
    feed(hub, s, "heartbeat", "agent", {}, NOW)
    (n,) = hub.evaluate("s1", NOW)
    assert "set aside" in n.body and n.severity == "warn"
    feed(hub, s, "heartbeat", "agent", {}, NOW + 86401)
    (r,) = hub.evaluate("s1", NOW + 86401)
    assert r.title == "Resolved: S1" and "no events set aside" in r.body


def test_removed_rule_resolves_its_alert() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "state", "door", {"open": True}, NOW)
    hub.evaluate("s1", NOW)
    hub.cfg.rules = [r for r in hub.cfg.rules if r.name != "Door open"]
    (n,) = hub.evaluate("s1", NOW + 30)
    assert n.title == "Resolved: S1" and "no longer checked" in n.body


def test_transient_state_inside_one_upload_is_reported() -> None:
    # Water came and went between two syncs: latest says dry, but say so.
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(
                name="Water on the floor",
                type="state",
                metric="water.present",
                sites=["*"],
                bad=True,
                severity="critical",
            )
        ],
    )
    evs = [s.event("state", "water", {"present": v}) for v in (False, True, False)]
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    (n,) = hub.after_ingest(res, NOW)
    assert n.severity == "critical" and "already cleared" in n.body and "cleared by" in n.body
    assert hub.evaluate("s1", NOW) == []  # and no alert left open


def test_upload_is_acked_even_if_rules_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    s = Site()
    hub = hub_with(s)

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("bug in a rule")

    monkeypatch.setattr(hub, "evaluate", boom)

    async def go() -> int:
        async with TestClient(TestServer(hub.make_app())) as client:
            r = await client.post(
                "/api/ingest", json=s.upload([s.event("reading", "batt", {"v": 13.0})])
            )
            return r.status

    assert asyncio.run(go()) == 200


def test_critical_reminder() -> None:
    s = Site()
    hub = hub_with(s, remind_critical=3600)
    feed(hub, s, "reading", "batt", {"v": 11.0}, NOW)
    hub.evaluate("s1", NOW)
    feed(hub, s, "reading", "batt", {"v": 11.0}, NOW + 1800)
    assert hub.evaluate("s1", NOW + 1800) == []
    feed(hub, s, "reading", "batt", {"v": 11.0}, NOW + 3700)
    assert hub.evaluate("s1", NOW + 3700)[0].title == "STILL CRITICAL: S1"


def test_rate_rule_catches_slow_decline() -> None:
    s = Site()
    rule = RuleConfig(
        name="Battery falling",
        type="rate",
        metric="batt.v",
        sites=["*"],
        window=7 * 86400,
        smooth=86400,
        drop=0.3,
        critical_drop=0.6,
        unit="V",
    )
    hub = hub_with(s, rules=[rule])
    # 9 days of hourly readings declining 0.06 V/day: 0.42 V per week.
    t0 = NOW - 9 * 86400
    for h in range(9 * 24):
        t = t0 + h * 3600
        hub.db.x(
            "INSERT INTO points (site, metric, t, value) VALUES ('s1', 'batt.v', ?, ?)",
            (t, 13.4 - 0.06 * (h / 24)),
        )
    hub.db.x(
        "INSERT INTO latest (site, metric, t, value) VALUES ('s1', 'batt.v', ?, '12.9')",
        (t0 + (9 * 24 - 1) * 3600,),
    )
    hub.db.x("INSERT INTO contact (site, last_recv, last_seq) VALUES ('s1', ?, 1)", (NOW,))
    (n,) = hub.evaluate("s1", NOW)
    assert n.severity == "warn" and "fell 0.42 V in 7 days" in n.body


def test_rate_rule_needs_history() -> None:
    s = Site()
    rule = RuleConfig(name="Battery falling", type="rate", metric="batt.v", sites=["*"], drop=0.3)
    hub = hub_with(s, rules=[rule])
    feed(hub, s, "reading", "batt", {"v": 10.0}, NOW)
    assert hub.evaluate("s1", NOW) == []


def test_escalate_after() -> None:
    s = Site()
    rule = RuleConfig(
        name="Door open",
        type="state",
        metric="door.open",
        sites=["*"],
        bad=True,
        escalate_after=600,
    )
    hub = hub_with(s, rules=[rule])
    feed(hub, s, "state", "door", {"open": True}, NOW)
    assert hub.evaluate("s1", NOW)[0].severity == "warn"
    feed(hub, s, "heartbeat", "agent", {}, NOW + 700)
    assert hub.evaluate("s1", NOW + 700)[0].severity == "critical"


def test_rule_site_patterns() -> None:
    r = RuleConfig(name="x", type="state", metric="m", sites=["mtn-*"], bad=True)
    assert rules.applies(r, "mtn-hilltop") and not rules.applies(r, "shack")


def test_state_rule_does_not_confuse_bool_and_number() -> None:
    r = RuleConfig(name="x", type="state", metric="m", sites=["*"], bad=True)
    assert rules.state_matches(r, True) and not rules.state_matches(r, 1)


class Receiver:
    def __init__(self) -> None:
        self.got: list[tuple[str, dict[str, str], bytes]] = []
        self.fail = 0

    def app(self) -> web.Application:
        async def h(req: web.Request) -> web.Response:
            if self.fail:
                self.fail -= 1
                return web.Response(status=503)
            self.got.append((req.path, dict(req.headers), await req.read()))
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/{tail:.*}", h)
        return app


async def test_outbox_delivers_and_retries() -> None:
    rx = Receiver()
    rx.fail = 1
    async with TestServer(rx.app()) as srv:
        s = Site()
        chans = [
            ChannelConfig(
                type="discord",
                name="discord",
                min_severity="warn",
                url=str(srv.make_url("/webhook")),
            ),
            ChannelConfig(
                type="ntfy",
                name="ntfy",
                min_severity="critical",
                url=str(srv.make_url("")).rstrip("/"),
                topic="site-alerts",
                token="tk",
            ),
        ]
        hub = hub_with(s, channels=chans)
        feed(hub, s, "reading", "batt", {"v": 12.0}, NOW)
        hub.evaluate("s1", NOW)  # warn: discord only
        assert await hub.flush_outbox(NOW) == 0  # receiver down once
        assert await hub.flush_outbox(NOW + 10) == 0  # backing off
        assert await hub.flush_outbox(NOW + 61) == 1
        feed(hub, s, "reading", "batt", {"v": 11.5}, NOW + 100)
        hub.evaluate("s1", NOW + 100)  # critical: both
        assert await hub.flush_outbox(NOW + 100) == 2
        await hub.http().close()
    discord = [json.loads(b) for p, _, b in rx.got if p == "/webhook"]
    assert discord[0]["allowed_mentions"] == {"parse": []}
    assert discord[0]["content"].startswith("**WARN: S1**")
    (ntfy,) = [(h, b) for p, h, b in rx.got if p == "/site-alerts"]
    assert ntfy[0]["Priority"] == "5" and ntfy[0]["Authorization"] == "Bearer tk"


def test_channel_group_and_severity_filter() -> None:
    ch = ChannelConfig(type="discord", name="d", min_severity="critical", groups=["hills"])
    assert alerts.wants(ch, "critical", "hills")
    assert not alerts.wants(ch, "warn", "hills")
    assert not alerts.wants(ch, "critical", "other")


def test_weekly_summary_text() -> None:
    s = Site()
    hub = hub_with(s)
    for i in range(5):
        feed(hub, s, "reading", "batt", {"v": 12.8 + i / 10}, NOW - 3600 * i)
    feed(hub, s, "state", "door", {"open": True}, NOW)
    hub.evaluate("s1", NOW)
    text = weekly_summary(hub.cfg, hub.db, "group", NOW + 1)
    assert "**S1**" in text and "batt battery/supply 12.8-13.2 V" in text
    assert "OPEN: Door open" in text


@pytest.mark.parametrize(("per_day", "words"), [(-0.02, "falling 0.14"), (0.006, "rising 0.04")])
def test_weekly_summary_reports_the_battery_trend(per_day: float, words: str) -> None:
    s = Site()
    hub = hub_with(s)
    for day in range(14):
        t = NOW - 86400 * (13 - day)
        feed_at(hub, s, "batt", {"v": 13.4}, t)
        feed_at(hub, s, "batt", {"v": 12.6 + per_day * day}, t + 3600)
    text = weekly_summary(hub.cfg, hub.db, "group", NOW + 7200)
    assert f"batt nightly low {words} V a week over 30 days" in text


DAY = 86400.0


def put_lows(db: HubDB, lows: list[float], end: float) -> None:
    """One low a day, the last on the day of ``end``, plus a charged reading
    each afternoon."""
    first = end - (len(lows) - 1) * DAY
    with db.tx() as c:
        for i, lo in enumerate(lows):
            c.execute("INSERT INTO points VALUES ('s1', 'batt.v', ?, ?)", (first + i * DAY, lo))
            c.execute("INSERT INTO points VALUES ('s1', 'batt.v', ?, ?)", (first + i * DAY + 1, 14))


def test_trend_needs_seven_whole_days() -> None:
    db = HubDB(":memory:")
    put_lows(db, [12.0 - 0.01 * i for i in range(8)], NOW)
    assert low_trend(db, "s1", "batt.v", NOW + 2) is None  # 6 whole days
    db = HubDB(":memory:")
    put_lows(db, [12.0 - 0.01 * i for i in range(9)], NOW)
    assert low_trend(db, "s1", "batt.v", NOW + 2) == pytest.approx(-0.07)


def test_trend_ignores_old_data_and_one_bad_sample() -> None:
    db = HubDB(":memory:")
    lows = [14.0 - 0.1 * i for i in range(20)] + [12.0] * 25  # a steep fall 25+ days ago
    lows[-5] = 0.0  # one glitch
    put_lows(db, lows, NOW)
    assert low_trend(db, "s1", "batt.v", NOW + 2) == pytest.approx(0.0)


def solar_volts(t: float) -> float:
    """12.2 V at 14:00 UTC, 14.2 V at 22:00, back down overnight; the same every day."""
    h = (t % DAY) / 3600
    if 14 <= h < 22:
        return 12.2 + 2.0 * (h - 14) / 8
    if h >= 22:
        return 14.2 - 2.0 * (h - 22) / 16
    return 14.2 - 2.0 * (h + 2) / 16


@pytest.mark.parametrize("hour", [4, 20])
def test_flat_solar_battery_is_steady_whatever_the_hour(hour: int) -> None:
    db = HubDB(":memory:")
    now = 20_040 * DAY + hour * 3600
    with db.tx() as c:
        c.executemany(
            "INSERT INTO points (site, metric, t, value) VALUES ('s1', 'batt.v', ?, ?)",
            [(t, solar_volts(t)) for t in (now - 45 * DAY + i * 1800 for i in range(45 * 48 + 1))],
        )
    trend = low_trend(db, "s1", "batt.v", now)
    assert trend is not None and abs(trend) < 0.005


def test_new_site_does_not_report_a_trend_from_its_first_part_day() -> None:
    db = HubDB(":memory:")
    start = 20_000 * DAY + 21 * 3600  # installed in the evening, the charge peak
    now = start + 9 * DAY
    with db.tx() as c:
        c.executemany(
            "INSERT INTO points (site, metric, t, value) VALUES ('s1', 'batt.v', ?, ?)",
            [(t, solar_volts(t)) for t in (start + i * 1800 for i in range(9 * 48 + 1))],
        )
    trend = low_trend(db, "s1", "batt.v", now)
    assert trend is not None and abs(trend) < 0.005


def test_battery_trend_needs_a_week_and_says_steady() -> None:
    s = Site()
    hub = hub_with(s)
    for day in range(5):
        feed_at(hub, s, "batt", {"v": 12.6}, NOW - 86400 * (4 - day))
    assert "nightly low" not in weekly_summary(hub.cfg, hub.db, "group", NOW + 1)
    for day in range(5, 10):
        feed_at(hub, s, "batt", {"v": 12.6}, NOW + 86400 * (day - 4))
    text = weekly_summary(hub.cfg, hub.db, "group", NOW + 86400 * 6 + 1)
    assert "batt nightly low steady over 30 days" in text


def test_summary_queued_once_at_configured_time() -> None:
    from datetime import datetime

    s = Site()
    chans = [ChannelConfig(type="discord", name="d", min_severity="warn", url="http://x")]
    hub = hub_with(s, channels=chans, summary_enabled=True)
    t = NOW
    while datetime.fromtimestamp(t).weekday() != 0 or datetime.fromtimestamp(t).hour != 8:
        t += 3600
    assert not hub.maybe_queue_summary(t - 3600)
    assert hub.maybe_queue_summary(t)
    assert not hub.maybe_queue_summary(t + 600)
    assert len(hub.db.q("SELECT id FROM outbox")) == 1


async def test_http_api_end_to_end() -> None:
    s = Site()
    hub = hub_with(s)
    async with TestClient(TestServer(hub.make_app())) as client:
        now = time.time()
        ev = Event.create(
            site="s1",
            seq=1,
            boot="b",
            kind="reading",
            source="batt",
            data={"v": 12.0},
            mono_ns=time.monotonic_ns(),
        ).signed(s.key)
        env: dict[str, Any] = {
            "site": "s1",
            "boot": "b",
            "mono_ns": time.monotonic_ns(),
            "wall": now,
            "events": [ev.to_dict()],
        }
        env["sig"] = s.key.sign(canonical_json(envelope_body(env)))
        r = await client.post("/api/ingest", json=env)
        assert r.status == 200 and (await r.json())["acked_through"] == 1

        env["sig"] = SigningKey.generate().sign(b"x")
        r = await client.post("/api/ingest", json=env)
        assert r.status == 403
        assert "s1" in hub.bad_uploads
        hub.evaluate("s1")
        assert not any(a.rule == "silence" for a in hub.db.open_alerts("s1"))

        group = await (await client.get("/api/group/group")).json()
        (tile,) = group["sites"]
        assert tile["status"] == "warn"  # 12.0 V is below the 12.2 V warning
        assert tile["latest"]["batt.v"]["value"] == 12.0
        assert "batt.v" in tile["spark"]

        r = await client.get("/api/csv", params={"site": "s1", "metric": "batt.v"})
        lines = (await r.text()).splitlines()
        assert lines[0] == "time_utc,unix_time,batt.v" and lines[1].endswith(",12.0")
        assert (await client.get("/api/csv", params={"site": "nope", "metric": "x"})).status == 400
        r = await client.get("/api/csv", params={"site": "s1", "metric": '=HYPERLINK("x")'})
        assert r.status == 404  # only metrics the site has reported
        r = await client.get("/api/csv", params={"site": "s1", "metric": "batt.v", "hours": "1e9"})
        assert r.status == 200
        assert r.headers["Content-Disposition"] == 'attachment; filename="s1_batt.v.csv"'

        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/group/group"
        assert "app.js" in await (await client.get("/group/group")).text()
        assert (await client.get("/static/../db.py")).status == 404
        assert (await client.get("/site/s1")).status == 200
        site = await (await client.get("/api/site/s1")).json()
        assert "batt.v" in site["metrics"] and site["days"] == 7

        r = await client.get("/api/site/s1", params={"days": "nan"})
        assert r.status == 400
        assert (await client.get("/api/site/s1", params={"days": "x"})).status == 400
        site = await (await client.get("/api/site/s1", params={"days": "1e9"})).json()
        assert site["days"] == 400  # the retention period
        for days in ("0", "-5"):
            assert (await (await client.get("/api/site/s1", params={"days": days})).json())[
                "days"
            ] == 1


async def test_site_page_range_reaches_older_readings_and_charts_rf() -> None:
    s = Site()
    hub = hub_with(s)
    now = time.time()
    for days_ago in range(24, -1, -1):
        feed_at(hub, s, "batt", {"v": 12.5 - 0.01 * (24 - days_ago)}, now - 86400 * days_ago)
    feed_at(hub, s, "rf", {"fwd_w": 48.0, "ref_w": 1.2, "swr": 1.37}, now - 60)
    async with TestClient(TestServer(hub.make_app())) as client:
        week = await (await client.get("/api/site/s1")).json()
        month = await (await client.get("/api/site/s1", params={"days": "30"})).json()
    assert len(week["spark"]["batt.v"]) == 7
    assert len(month["spark"]["batt.v"]) == 25
    assert len(week["spark"]["rf.fwd_w"]) == 1 and len(week["spark"]["rf.swr"]) == 1
    assert week["trends"]["batt.v"] == pytest.approx(-0.07)


class FakeSMTP:
    """Stands in for the SMTP server; login encodes the password the way
    smtplib does for AUTH, so a non-ASCII one fails the same way."""

    sent: list[str] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        pass

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def starttls(self, context: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        password.encode("ascii")

    def send_message(self, msg: Any) -> None:
        FakeSMTP.sent.append(msg["Subject"])


async def test_bad_email_password_is_retried_without_blocking_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    rx = Receiver()
    async with TestServer(rx.app()) as srv:
        s = Site()
        chans = [
            ChannelConfig(
                type="email",
                name="email",
                min_severity="warn",
                smtp_host="mail",
                smtp_user="u",
                smtp_password="p\u00e4ss",
                sender="a@x",
                to=["b@x"],
            ),
            ChannelConfig(
                type="discord",
                name="discord",
                min_severity="warn",
                url=str(srv.make_url("/webhook")),
            ),
        ]
        hub = hub_with(s, channels=chans)
        feed(hub, s, "reading", "batt", {"v": 12.0}, NOW)
        hub.evaluate("s1", NOW)
        assert await hub.flush_outbox(NOW) == 1
        await hub.http().close()
    assert [p for p, _, _ in rx.got] == ["/webhook"]
    row = hub.db.q1("SELECT tries, last_error FROM outbox WHERE channel='email'")
    assert row is not None and row["tries"] == 1 and "UnicodeEncodeError" in row["last_error"]


async def test_slow_channel_does_not_delay_the_others() -> None:
    import asyncio

    stall = asyncio.Event()
    got: list[str] = []

    async def slow(req: web.Request) -> web.Response:
        await stall.wait()
        return web.Response(status=204)

    async def fast(req: web.Request) -> web.Response:
        got.append(req.headers["Title"])
        if len(got) == 2:
            stall.set()  # discord is released only once ntfy has both messages
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/webhook", slow)
    app.router.add_post("/topic", fast)
    async with TestServer(app) as srv:
        s = Site()
        chans = [
            ChannelConfig(
                type="discord",
                name="discord",
                min_severity="warn",
                url=str(srv.make_url("/webhook")),
            ),
            ChannelConfig(
                type="ntfy",
                name="ntfy",
                min_severity="warn",
                url=str(srv.make_url("")).rstrip("/"),
                topic="topic",
            ),
        ]
        hub = hub_with(s, channels=chans)
        feed(hub, s, "reading", "batt", {"v": 12.0}, NOW)
        hub.evaluate("s1", NOW)
        feed(hub, s, "reading", "batt", {"v": 11.0}, NOW + 30)
        hub.evaluate("s1", NOW + 30)
        assert await asyncio.wait_for(hub.flush_outbox(NOW + 30), 10) == 4
        await hub.http().close()
    # in order within the channel
    assert got == ["WARN: S1", "ESCALATED to CRITICAL: S1"]


def test_hold_ignores_a_short_dip() -> None:
    s = Site()
    rule = RuleConfig(
        name="Battery low",
        type="threshold",
        metric="batt.v",
        sites=["*"],
        below=12.2,
        unit="V",
        hold=120,
    )
    hub = hub_with(s, rules=[rule])
    # a transmitter key-up sags the battery for one reading
    for i, v in enumerate([12.5, 11.9, 12.5, 11.9]):
        feed(hub, s, "reading", "batt", {"v": v}, NOW + 30 * i)
        assert hub.evaluate("s1", NOW + 30 * i) == []
    # low and staying low: the alert waits out the hold, then opens
    notices = []
    for i in range(4, 10):
        feed(hub, s, "reading", "batt", {"v": 11.9}, NOW + 30 * i)
        notices += hub.evaluate("s1", NOW + 30 * i)
    assert [n.title for n in notices] == ["WARN: S1"]
    assert "11.9 V" in notices[0].body


def test_transient_shorter_than_hold_is_not_reported() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(
                name="Door open", type="state", metric="door.open", sites=["*"], bad=True, hold=60
            )
        ],
    )
    evs = [s.event("state", "door", {"open": v}) for v in (True, False)]  # 30 s apart
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    assert hub.after_ingest(res, NOW) == []


def test_state_snapshot_drops_keys_the_source_no_longer_sends(hubdb: HubDB) -> None:
    s = Site()
    first = s.event("state", "ami", {"connected": True, "registered": False})
    ingest(hubdb, keys_for(s), s.upload([first]), now=NOW)
    s.event("fault", "ami", {"ok": True})  # unrelated, never uploaded
    later = s.event("state", "ami", {"connected": True, "link_count": 2})
    ingest(hubdb, keys_for(s), s.upload([later]), now=NOW + 30)
    latest = hubdb.latest("s1")
    assert "ami.registered" not in latest and latest["ami.link_count"][0] == 2
    # a different source with a similar name is untouched
    other = s.event("state", "ami_x", {"z": 1})
    ingest(hubdb, keys_for(s), s.upload([other]), now=NOW + 40)
    assert "ami.connected" in hubdb.latest("s1")
