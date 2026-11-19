from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sitemon import SigningKey
from sitemon.agent.store import SEQ_BLOCK, EventLog

HUB = "http://hub.local.mesh:8470"
MIRROR = "https://hub.example.org"


def test_events_reach_disk_only_on_flush(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB])
    log.append("reading", "batt", {"v": 13.0})
    assert log.unsent(HUB) == []
    assert log.depth() == 1 and log.lag(HUB) == 1
    assert log.flush() == 1
    assert [e.data for e in log.unsent(HUB)] == [{"v": 13.0}]


def test_seq_never_repeats_after_a_crash(tmp_path: Path, key: SigningKey) -> None:
    path = str(tmp_path / "e.db")
    log = EventLog(path, "s1", key, hubs=[HUB])
    seqs = [log.append("reading", "batt", {"v": 1.0}).seq for _ in range(5)]
    # Simulated power cut: buffered events are lost, the process dies unflushed.
    log._db.close()
    log2 = EventLog(path, "s1", key, hubs=[HUB])
    nxt = log2.append("reading", "batt", {"v": 2.0}).seq
    assert nxt > max(seqs)
    assert nxt > SEQ_BLOCK  # resumed above the reserved block
    assert log2.boot != log.boot


def test_seq_continues_after_clean_restart(tmp_path: Path, key: SigningKey) -> None:
    path = str(tmp_path / "e.db")
    log = EventLog(path, "s1", key, hubs=[HUB])
    last = [log.append("reading", "batt", {"v": 1.0}) for _ in range(3)][-1]
    log.close()
    assert EventLog(path, "s1", key, hubs=[HUB]).append("reading", "batt", {}).seq > last.seq


def test_failed_reservation_is_retried_not_skipped(
    tmp_path: Path, key: SigningKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "e.db")
    log = EventLog(path, "s1", key, hubs=[HUB])
    real = log._set_meta
    failures = [sqlite3.OperationalError("disk I/O error")]

    def flaky(k: str, v: int) -> None:
        if k == "seq_reserved" and failures:
            raise failures.pop()
        real(k, v)

    monkeypatch.setattr(log, "_set_meta", flaky)
    with pytest.raises(sqlite3.OperationalError):
        log.append("reading", "batt", {"i": 0})
    # The next event must not ride on the block whose bound never reached disk.
    ev = log.append("reading", "batt", {"i": 1})
    log.flush()
    log.ack(HUB, ev.seq)  # the hub has it, so the site keeps no copy
    log._db.close()  # power cut
    assert EventLog(path, "s1", key, hubs=[HUB]).append("reading", "batt", {}).seq > ev.seq


def test_failed_flush_rolls_back_and_keeps_the_batch(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB])
    evs = [log.append("reading", "batt", {"i": i}) for i in range(3)]
    # The second row of the batch fails, after the first went in.
    log._db.execute(
        f"CREATE TRIGGER io_error BEFORE INSERT ON events WHEN NEW.seq = {evs[1].seq} "
        "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="disk I/O error"):
        log.flush()
    assert not log._db.in_transaction
    assert log.unsent(HUB) == []  # the first row was rolled back with the rest
    assert log.depth() == 3 and log.dropped == 0
    log.append("reading", "batt", {"i": 3})
    log._db.execute("DROP TRIGGER io_error")
    assert log.flush() == 4
    assert [e.data["i"] for e in log.unsent(HUB)] == [0, 1, 2, 3]


def test_buffer_is_capped_while_the_card_fails(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB], max_events=3)
    log._db.execute(
        "CREATE TRIGGER io_error BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'dead'); END"
    )
    for i in range(5):
        log.append("reading", "batt", {"i": i})
    with pytest.raises(sqlite3.DatabaseError):
        log.flush()
    assert log.depth() == 3 and log.dropped == 2
    log._db.execute("DROP TRIGGER io_error")
    log.flush()
    assert [e.data["i"] for e in log.unsent(HUB)] == [2, 3, 4]  # oldest went first


def test_ack_removes_only_acknowledged(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB])
    evs = [log.append("reading", "batt", {"i": i}) for i in range(10)]
    log.flush()
    log.ack(HUB, evs[3].seq)
    assert [e.data["i"] for e in log.unsent(HUB)] == list(range(4, 10))
    assert len(log.unsent(HUB, limit=2)) == 2
    assert log.depth() == log.lag(HUB) == 6


def test_every_hub_gets_every_event(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB, MIRROR])
    evs = [log.append("reading", "batt", {"i": i}) for i in range(6)]
    log.flush()
    assert log.unsent(HUB) == log.unsent(MIRROR) == evs

    # The mirror is unreachable; the mesh hub carries on alone.
    log.ack(HUB, evs[-1].seq)
    assert log.unsent(HUB) == [] and log.lag(HUB) == 0
    assert log.unsent(MIRROR) == evs and log.lag(MIRROR) == 6
    assert log.depth() == 6  # nothing deleted: the mirror doesn't have them yet

    # The mirror catches up in order; rows go once both hubs have them.
    log.ack(MIRROR, evs[2].seq)
    assert log.unsent(MIRROR) == evs[3:]
    assert log.depth() == 3
    assert log._db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    log.ack(MIRROR, evs[-1].seq)
    assert log.depth() == 0
    assert log._db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_cursors_survive_a_restart_and_never_go_back(tmp_path: Path, key: SigningKey) -> None:
    path = str(tmp_path / "e.db")
    log = EventLog(path, "s1", key, hubs=[HUB, MIRROR])
    evs = [log.append("reading", "batt", {"i": i}) for i in range(5)]
    log.flush()
    log.ack(HUB, evs[3].seq)
    log.ack(HUB, evs[1].seq)  # a late answer to an earlier upload
    assert log.unsent(HUB) == evs[4:]
    log.close()
    log = EventLog(path, "s1", key, hubs=[HUB, MIRROR])
    assert log.unsent(HUB) == evs[4:]
    assert log.unsent(MIRROR) == evs


def test_removing_a_hub_from_the_config_lets_its_backlog_go(
    tmp_path: Path, key: SigningKey
) -> None:
    path = str(tmp_path / "e.db")
    log = EventLog(path, "s1", key, hubs=[HUB, MIRROR])
    evs = [log.append("reading", "batt", {"i": i}) for i in range(4)]
    log.flush()
    log.ack(HUB, evs[-1].seq)
    assert log.depth() == 4  # held for the mirror, which never answers
    log.close()
    log = EventLog(path, "s1", key, hubs=[HUB])  # restarted without it
    assert log.depth() == 0
    assert log._db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_without_hubs_only_the_cap_deletes(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[], max_events=5)
    evs = [log.append("reading", "batt", {"i": i}) for i in range(4)]
    log.flush()
    log.ack(HUB, evs[-1].seq)  # not a configured hub: changes nothing here
    assert log.depth() == 4
    for i in range(4, 8):
        log.append("reading", "batt", {"i": i})
    log.flush()
    assert log.depth() == 5 and log.dropped == 3


def test_cap_drops_oldest(tmp_path: Path, key: SigningKey) -> None:
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB], max_events=5)
    for i in range(8):
        log.append("reading", "batt", {"i": i})
    log.flush()
    assert [e.data["i"] for e in log.unsent(HUB)] == [3, 4, 5, 6, 7]
    assert log.dropped == 3


def test_stored_events_stay_signed(tmp_path: Path) -> None:
    key = SigningKey.generate()
    log = EventLog(str(tmp_path / "e.db"), "s1", key, hubs=[HUB])
    log.append("state", "door", {"open": True})
    log.flush()
    assert log.unsent(HUB)[0].verify(key.public_key())
