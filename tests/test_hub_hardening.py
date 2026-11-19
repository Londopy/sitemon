"""Regression tests from code review: each test pins one finding."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sitemon._events import canonical_json, envelope_body
from sitemon.config import ChannelConfig, ConfigError, RuleConfig, load_hub_config
from sitemon.hub.db import HubDB
from sitemon.hub.ingest import IngestError, ingest

from .test_hub import NOW, Receiver, Site, feed, hub_with, keys_for


def resign(s: Site, up: dict[str, Any]) -> dict[str, Any]:
    up["sig"] = s.key.sign(canonical_json(envelope_body(up)))
    return up


# C1: the envelope signature covers every event byte for byte.
def test_event_swapped_in_transit_is_refused(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload([s.event("reading", "batt", {"v": 13.0}) for _ in range(3)])
    up["events"][1] = {"seq": 2, "garbage": True}  # sig left as the site made it
    with pytest.raises(IngestError) as ei:
        ingest(hubdb, keys_for(s), up, now=NOW)
    assert ei.value.status == 403
    assert hubdb.latest("s1") == {} and hubdb.rejected_since("s1", 0) == []


def test_unverified_seq_never_becomes_the_counter_baseline(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload([s.event("reading", "batt", {"v": 13.0})])
    up["events"].append({"seq": 10**12, "junk": 1})  # the site's own bug, say
    res = ingest(hubdb, keys_for(s), resign(s, up), now=NOW)
    assert res.acked_through == 10**12  # the envelope says so, and it's signed
    row = hubdb.q1("SELECT last_seq FROM contact WHERE site='s1'")
    assert row is not None and row["last_seq"] == 1


def test_replayed_upload_adds_no_quarantine_rows(hubdb: HubDB) -> None:
    s = Site()
    up = s.upload([s.event("reading", "batt", {"v": 13.0})])
    up["events"].append({"seq": 2, "junk": 1})
    up = resign(s, up)
    for i in range(5):
        ingest(hubdb, keys_for(s), up, now=NOW + i)
    assert len(hubdb.rejected_since("s1", 0)) == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("seq", 2**63),
        ("mono_ns", 2**64),
        ("wall", float("nan")),
        ("wall", float("inf")),
    ],
)
def test_out_of_range_fields_are_quarantined_not_500(hubdb: HubDB, field: str, value: Any) -> None:
    s = Site()
    good = s.event("reading", "batt", {"v": 13.0})
    bad = s.event("reading", "env", {"temp_c": 1.0}).to_dict()
    bad[field] = value
    up = resign(s, s.upload([good]) | {"events": [good.to_dict(), bad]})
    res = ingest(hubdb, keys_for(s), up, now=NOW)
    assert len(res.stored) == 1 and len(res.quarantined) == 1


def test_huge_integer_in_data_is_quarantined(hubdb: HubDB) -> None:
    s = Site()
    ev = s.event("reading", "batt", {"v": 10**400})
    res = ingest(hubdb, keys_for(s), s.upload([ev]), now=NOW)
    assert res.stored == [] and len(res.quarantined) == 1


# C2: a restart doesn't resolve and re-raise a silence that's already open.
def test_hub_restart_keeps_an_open_silence_alarm() -> None:
    s = Site()
    hub = hub_with(s, silence_warn=300, silence_critical=900)
    hub.first_configured["s1"] = hub.process_started = NOW - 86400
    feed(hub, s, "heartbeat", "agent", {}, NOW - 86400)
    assert hub.evaluate("s1", NOW)[0].severity == "critical"
    hub.process_started = NOW + 10  # the hub restarts; the site is still dead
    assert hub.evaluate("s1", NOW + 20) == []
    assert hub.db.open_alert("s1", "silence") is not None


# C3: an alert whose metric is no longer reported resolves.
def test_alert_resolves_when_its_metric_disappears() -> None:
    s = Site()
    rule = RuleConfig(
        name="Not registered",
        type="state",
        metric="ami.registered",
        sites=["*"],
        bad=False,
        severity="critical",
    )
    hub = hub_with(s, rules=[rule])
    feed(hub, s, "state", "ami", {"connected": True, "registered": False}, NOW)
    assert hub.evaluate("s1", NOW)[0].severity == "critical"
    feed(hub, s, "state", "ami", {"connected": True}, NOW + 30)  # check turned off
    (n,) = hub.evaluate("s1", NOW + 30)
    assert n.title == "Resolved: S1" and "no longer reported" in n.body


# C4: after one real counter reset, ordinary restarts aren't flagged.
def test_only_the_real_counter_reset_is_flagged(hubdb: HubDB) -> None:
    s = Site(boot="old-card")
    s.seq = 5000
    ingest(hubdb, keys_for(s), s.upload([s.event("reading", "batt", {"v": 13.0})]), now=NOW)
    new = Site(key=s.key, boot="new-card")
    res = ingest(
        hubdb, keys_for(new), new.upload([new.event("reading", "batt", {"v": 13.0})]), now=NOW + 60
    )
    assert res.counter_reset
    for i in range(3):  # ordinary restarts: the counter carries on from the new card
        new.boot = f"restart-{i}"
        new.seq += 1000
        res = ingest(
            hubdb,
            keys_for(new),
            new.upload([new.event("reading", "batt", {"v": 13.0})]),
            now=NOW + 120 + i,
        )
        assert not res.counter_reset


# C5: charts and CSV survive a key dropping out of latest.
async def test_history_stays_reachable_after_a_key_drops_out_of_latest() -> None:
    s = Site()
    hub = hub_with(s)
    feed(hub, s, "state", "ami", {"connected": True, "link_count": 3}, NOW)
    feed(hub, s, "state", "ami", {"connected": False, "error": "refused"}, NOW + 30)
    assert "ami.link_count" not in hub.db.latest("s1")
    async with TestClient(TestServer(hub.make_app())) as client:
        r = await client.get(
            "/api/csv",
            params={
                "site": "s1",
                "metric": "ami.link_count",
                "from": str(NOW - 60),
                "to": str(NOW + 60),
            },
        )
        assert r.status == 200 and (await r.text()).count("\n") == 2


# C6: pruning in small chunks still removes everything old.
def test_prune_in_chunks() -> None:
    db = HubDB(":memory:")
    db._db.executemany(
        "INSERT INTO points (site, metric, t, value) VALUES ('s', 'm', ?, 1)",
        [(float(t),) for t in range(1000)],
    )
    assert db.prune(before=900, chunk=37) == 900
    assert len(db.q("SELECT t FROM points")) == 100


# C9: a failed COMMIT doesn't leave the connection stuck in a transaction.
def test_failed_commit_rolls_back() -> None:
    db = HubDB(":memory:")
    db._db.execute("PRAGMA foreign_keys=ON")
    db._db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    db._db.execute(
        "CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(Exception):
        with db.tx() as c:
            c.execute("INSERT INTO child (pid) VALUES (42)")  # fails only at COMMIT
    assert not db._db.in_transaction
    with db.tx() as c:  # and the next transaction works
        c.execute("INSERT INTO parent (id) VALUES (1)")


# C11: an older snapshot arriving late doesn't bring back dropped keys.
def test_late_older_snapshot_does_not_resurrect_keys(hubdb: HubDB) -> None:
    s = Site()
    old = s.event("state", "ami", {"connected": True, "registered": False})
    new = s.event("state", "ami", {"connected": True})
    ingest(hubdb, keys_for(s), s.upload([new]), now=NOW)
    ingest(hubdb, keys_for(s), s.upload([old]), now=NOW + 5)
    assert "ami.registered" not in hubdb.latest("s1")


# C12: duplicate rule names are a config error.
def test_duplicate_rule_names_rejected(tmp_path: Path) -> None:
    p = tmp_path / "hub.toml"
    p.write_text(
        '[[rules]]\nname="x"\nmetric="a.b"\nbelow=1\n[[rules]]\nname="x"\nmetric="c.d"\nbelow=2\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate rule name"):
        load_hub_config(str(p))


DOOR = RuleConfig(
    name="Door open", type="state", metric="door.open", sites=["*"], bad=True, hold=120
)


def test_hold_on_a_state_rule_counts_site_time_not_state_resends() -> None:
    # States are only re-sent every few minutes; heartbeats keep site time moving.
    s = Site()
    hub = hub_with(s, rules=[DOOR])

    def send(kind: str, source: str, data: dict[str, Any], at: float, advance: float) -> None:
        # the site's monotonic clock and the hub's clock move together
        ingest(
            hub.db, hub.keys, s.upload([s.event(kind, source, data, advance=advance)]), now=NOW + at
        )

    send("state", "door", {"open": False}, -60, 30)
    send("state", "door", {"open": True}, 0, 60)
    send("heartbeat", "agent", {}, 60, 60)
    assert hub.evaluate("s1", NOW + 60) == []
    send("heartbeat", "agent", {}, 110, 50)
    assert hub.evaluate("s1", NOW + 110) == []
    send("heartbeat", "agent", {}, 125, 15)
    (n,) = hub.evaluate("s1", NOW + 125)
    assert n.title == "WARN: S1" and "Door open" in n.body


def test_hold_counts_a_backlog_by_reading_time() -> None:
    # Six hours of low readings arrive in one upload after an outage.
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
    evs = [s.event("reading", "batt", {"v": 11.9}, advance=1800) for _ in range(12)]
    ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    (n,) = hub.evaluate("s1", NOW)
    assert n.title == "WARN: S1"


def test_a_good_reading_restarts_the_hold() -> None:
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
    evs = [s.event("reading", "batt", {"v": v}) for v in (11.9, 11.9, 11.9, 12.6, 11.9, 11.9)]
    ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    assert hub.evaluate("s1", NOW) == []  # only 30 s low since the 12.6 V reading


def test_transient_checks_every_bad_run() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(
                name="Door open", type="state", metric="door.open", sites=["*"], bad=True, hold=60
            )
        ],
    )
    evs = [
        s.event("state", "door", {"open": True}, advance=0),
        s.event("state", "door", {"open": False}, advance=10),  # a 10 s blip
        s.event("state", "door", {"open": True}, advance=20),
        s.event("state", "door", {"open": False}, advance=480),  # open 8 minutes
    ]
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    (n,) = hub.after_ingest(res, NOW)
    assert "already cleared" in n.body


async def test_failed_message_holds_its_channel_in_order() -> None:
    rx = Receiver()
    rx.fail = 1
    async with TestServer(rx.app()) as srv:
        s = Site()
        hub = hub_with(
            s,
            channels=[
                ChannelConfig(
                    type="discord", name="d", min_severity="warn", url=str(srv.make_url("/webhook"))
                )
            ],
        )
        for i in range(2):
            hub.db.x(
                "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
                "VALUES ('d', ?, ?, 'warn', ?, 'x')",
                (NOW, NOW, f"msg {i}"),
            )
        assert await hub.flush_outbox(NOW) == 0
        assert rx.got == []  # msg 1 must not overtake msg 0
        assert await hub.flush_outbox(NOW + 61) == 2
        await hub.http().close()
    titles = [b.decode().split("**")[1] for _, _, b in rx.got]
    assert titles == ["msg 0", "msg 1"]


async def test_dead_channel_backlog_does_not_starve_others() -> None:
    rx = Receiver()
    async with TestServer(rx.app()) as srv:
        s = Site()
        chans = [
            ChannelConfig(
                type="discord", name="dead", min_severity="warn", url="http://127.0.0.1:9/never"
            ),
            ChannelConfig(
                type="discord", name="ok", min_severity="warn", url=str(srv.make_url("/webhook"))
            ),
        ]
        hub = hub_with(s, channels=chans)
        for i in range(300):
            hub.db.x(
                "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
                "VALUES ('dead', ?, ?, 'warn', ?, 'x')",
                (NOW, NOW, f"dead {i}"),
            )
        await hub.flush_outbox(NOW)
        hub.db.x(
            "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
            "VALUES ('ok', ?, ?, 'warn', 'fresh', 'x')",
            (NOW + 10, NOW + 10),
        )
        assert await hub.flush_outbox(NOW + 10) == 1
        await hub.http().close()
    assert len(rx.got) == 1


async def test_unsendable_message_backs_off_instead_of_crashing() -> None:
    rx = Receiver()
    async with TestServer(rx.app()) as srv:
        s = Site()
        hub = hub_with(
            s,
            channels=[
                ChannelConfig(
                    type="ntfy",
                    name="n",
                    min_severity="warn",
                    url=str(srv.make_url("")).rstrip("/"),
                    topic="t",
                )
            ],
        )
        hub.db.x(
            "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
            "VALUES ('n', ?, ?, 'warn', ?, 'x')",
            (NOW, NOW, "bad\nname"),
        )
        assert await hub.flush_outbox(NOW) == 0
        await hub.http().close()
    row = hub.db.q1("SELECT tries, last_error FROM outbox")
    assert row is not None and row["tries"] == 1 and "ValueError" in row["last_error"]


async def test_hub_loop_survives_a_database_error() -> None:
    s = Site()
    hub = hub_with(s)
    calls = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database or disk is full")
        hub.stop()

    await asyncio.wait_for(hub._every(0, flaky), 5)
    assert len(calls) == 2


async def test_hub_run_stops_all_loops_on_a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Site()
    hub = hub_with(s)

    def boom(now: float | None = None) -> list[Any]:
        raise RuntimeError("rule bug")

    monkeypatch.setattr(hub, "evaluate_all", boom)
    with pytest.raises(RuntimeError, match="rule bug"):
        await asyncio.wait_for(hub.run(), 5)
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert left == []


def readings(hub: Any, s: Site, metric: str, values: list[float], step: float = 30) -> None:
    """One upload of readings ``step`` seconds apart, ending at NOW."""
    src, _, key = metric.partition(".")
    evs = [s.event("reading", src, {key: v}, advance=step) for v in values]
    ingest(hub.db, hub.keys, s.upload(evs), now=NOW)


HOT = RuleConfig(
    name="Too hot",
    type="threshold",
    metric="env.temp_c",
    sites=["*"],
    above=40,
    critical_above=50,
    unit="C",
    hold=120,
)


def test_hold_on_above_levels() -> None:
    s = Site()
    hub = hub_with(s, rules=[HOT])
    readings(hub, s, "env.temp_c", [30, 45, 30, 45])  # brief spikes only
    assert hub.evaluate("s1", NOW) == []
    s2 = Site()
    hub2 = hub_with(s2, rules=[HOT])
    readings(hub2, s2, "env.temp_c", [30, 45, 45, 45, 45, 45])
    (n,) = hub2.evaluate("s1", NOW)
    assert n.severity == "warn"


def test_one_critical_spike_does_not_escalate_a_held_rule() -> None:
    s = Site()
    hub = hub_with(s, rules=[HOT])
    readings(hub, s, "env.temp_c", [45] * 6)
    assert hub.evaluate("s1", NOW)[0].severity == "warn"
    feed(hub, s, "reading", "env", {"temp_c": 55}, NOW + 30)  # one spike
    assert hub.evaluate("s1", NOW + 30) == []
    a = hub.db.open_alert("s1", "rule:Too hot")
    assert a is not None and a.severity == "warn"


def test_critical_held_long_enough_escalates() -> None:
    s = Site()
    hub = hub_with(s, rules=[HOT])
    readings(hub, s, "env.temp_c", [45, 45, 45, 45, 55, 55, 55, 55, 55])
    (n,) = hub.evaluate("s1", NOW)
    assert n.severity == "critical"


def test_hold_with_only_a_critical_level() -> None:
    s = Site()
    rule = RuleConfig(
        name="Battery flat",
        type="threshold",
        metric="batt.v",
        sites=["*"],
        critical_below=11.0,
        unit="V",
        hold=120,
    )
    hub = hub_with(s, rules=[rule])
    readings(hub, s, "batt.v", [12.5, 10.5, 12.5])
    assert hub.evaluate("s1", NOW) == []
    readings(hub, s, "batt.v", [10.5] * 6)
    assert hub.evaluate("s1", NOW)[0].severity == "critical"


def test_hold_on_a_numeric_state() -> None:
    s = Site()
    rule = RuleConfig(
        name="One link left", type="state", metric="ami.link_count", sites=["*"], bad=1, hold=120
    )
    hub = hub_with(s, rules=[rule])
    evs = [s.event("state", "ami", {"link_count": n}) for n in (2, 1, 2)]
    ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    assert hub.evaluate("s1", NOW) == []
    evs = [s.event("state", "ami", {"link_count": 1})] + [
        s.event("heartbeat", "agent", {}) for _ in range(5)
    ]
    ingest(hub.db, hub.keys, s.upload(evs), now=NOW + 180)
    assert hub.evaluate("s1", NOW + 180)[0].title == "WARN: S1"


def test_short_backlog_respects_the_hold() -> None:
    s = Site()
    rule = RuleConfig(
        name="Battery low",
        type="threshold",
        metric="batt.v",
        sites=["*"],
        below=12.2,
        unit="V",
        hold=600,
    )
    hub = hub_with(s, rules=[rule])
    readings(hub, s, "batt.v", [12.6, 11.9, 11.9, 11.9])  # low for 60 s only
    assert hub.evaluate("s1", NOW) == []


def test_transient_reported_even_when_open_again_at_the_end() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(
                name="Door open", type="state", metric="door.open", sites=["*"], bad=True, hold=120
            )
        ],
    )
    evs = [
        s.event("state", "door", {"open": True}, advance=0),
        s.event("state", "door", {"open": False}, advance=600),  # open 10 minutes
        s.event("state", "door", {"open": True}, advance=20),  # open again, briefly so far
    ]
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    (n,) = hub.after_ingest(res, NOW)
    assert "cleared by" in n.body


def test_transient_spanning_two_uploads() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(
                name="Door open", type="state", metric="door.open", sites=["*"], bad=True, hold=120
            )
        ],
    )
    first = ingest(hub.db, hub.keys, s.upload([s.event("state", "door", {"open": True})]), now=NOW)
    assert hub.after_ingest(first, NOW) == []
    assert hub.evaluate("s1", NOW) == []  # too soon to alert
    # the link drops; the next upload only has the door shutting 251 s later
    later = ingest(
        hub.db,
        hub.keys,
        s.upload([s.event("state", "door", {"open": False}, advance=251)]),
        now=NOW + 251,
    )
    (n,) = hub.after_ingest(later, NOW + 251)
    assert "cleared by" in n.body


def test_no_transient_while_the_rule_alert_is_open() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(name="Door open", type="state", metric="door.open", sites=["*"], bad=True)
        ],
    )
    feed(hub, s, "state", "door", {"open": True}, NOW)
    hub.evaluate("s1", NOW)
    evs = [s.event("state", "door", {"open": v}) for v in (False, True)]
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW + 60)
    assert hub.after_ingest(res, NOW + 60) == []


def test_no_transient_for_a_run_still_going() -> None:
    s = Site()
    hub = hub_with(
        s,
        rules=[
            RuleConfig(name="Door open", type="state", metric="door.open", sites=["*"], bad=True)
        ],
    )
    evs = [s.event("state", "door", {"open": v}) for v in (False, True)]
    res = ingest(hub.db, hub.keys, s.upload(evs), now=NOW)
    assert hub.after_ingest(res, NOW) == []  # the rule alerts for this one


async def test_backoff_grows_and_is_capped() -> None:
    rx = Receiver()
    rx.fail = 100
    async with TestServer(rx.app()) as srv:
        s = Site()
        hub = hub_with(
            s,
            channels=[
                ChannelConfig(
                    type="discord", name="d", min_severity="warn", url=str(srv.make_url("/w"))
                )
            ],
        )
        hub.db.x(
            "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
            "VALUES ('d', ?, ?, 'warn', 't', 'x')",
            (NOW, NOW),
        )
        t = NOW
        delays = []
        for _ in range(6):
            await hub.flush_outbox(t)
            row = hub.db.q1("SELECT next_try FROM outbox")
            assert row is not None
            delays.append(row["next_try"] - t)
            t = row["next_try"]
        await hub.http().close()
    assert delays == [60, 120, 240, 300, 300, 300]


async def test_gives_up_on_a_message_no_retry_can_send() -> None:
    rx = Receiver()
    async with TestServer(rx.app()) as srv:
        s = Site()
        hub = hub_with(
            s,
            channels=[
                ChannelConfig(
                    type="ntfy",
                    name="n",
                    min_severity="warn",
                    url=str(srv.make_url("")).rstrip("/"),
                    topic="t",
                )
            ],
        )
        for title in ("bad\nname", "fine"):
            hub.db.x(
                "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
                "VALUES ('n', ?, ?, 'warn', ?, 'x')",
                (NOW, NOW, title),
            )
        assert await hub.flush_outbox(NOW) == 1  # the next message isn't held back
        await hub.http().close()
    rows = hub.db.q("SELECT title, done, last_error FROM outbox ORDER BY id")
    assert rows[0]["done"] is not None and rows[0]["last_error"].startswith("gave up")
    assert [h["Title"] for _, h, _ in rx.got] == ["fine"]


async def test_gives_up_after_a_day() -> None:
    rx = Receiver()
    rx.fail = 1
    async with TestServer(rx.app()) as srv:
        s = Site()
        hub = hub_with(
            s,
            channels=[
                ChannelConfig(
                    type="discord", name="d", min_severity="warn", url=str(srv.make_url("/w"))
                )
            ],
        )
        hub.db.x(
            "INSERT INTO outbox (channel, created, next_try, severity, title, body) "
            "VALUES ('d', ?, ?, 'warn', 'old', 'x')",
            (NOW - 90000, NOW),
        )
        await hub.flush_outbox(NOW)
        await hub.http().close()
    row = hub.db.q1("SELECT done, last_error FROM outbox")
    assert row is not None and row["done"] == NOW and row["last_error"].startswith("gave up")


def test_hold_is_refused_where_it_cannot_work(tmp_path: Path) -> None:
    for extra in ('type="rate"\ndrop=0.3\n', 'type="state"\nbad="refused"\n'):
        p = tmp_path / "hub.toml"
        p.write_text(f'[[rules]]\nname="x"\nmetric="a.b"\nhold="2m"\n{extra}', encoding="utf-8")
        with pytest.raises(ConfigError, match="hold works on"):
            load_hub_config(str(p))


def test_env_secrets_are_stripped_and_must_not_be_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "hub.toml"
    p.write_text('[[channels]]\ntype="discord"\nurl_env="HOOK"\n', encoding="utf-8")
    monkeypatch.setenv("HOOK", "https://discord.example/x\n")
    assert load_hub_config(str(p)).channels[0].url == "https://discord.example/x"
    monkeypatch.setenv("HOOK", "  ")
    with pytest.raises(ConfigError, match="is empty"):
        load_hub_config(str(p))
