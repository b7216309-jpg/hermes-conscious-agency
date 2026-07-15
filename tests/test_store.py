from __future__ import annotations

from dataclasses import replace

import pytest

from agency.store import AgencyStore


def test_state_and_ledgers_round_trip(config):
    store = AgencyStore(config)
    store.set_meta("sample", {"alive": True})
    assert store.get_meta("sample") == {"alive": True}

    event_id = store.add_event("test", summary="hello", metadata={"n": 2})
    assert store.recent_events(1)[0]["id"] == event_id
    assert store.recent_events(1)[0]["metadata"] == {"n": 2}

    intention = store.add_intention("Build a reliable loop", priority=80, autonomy="message")
    assert intention["status"] == "active"
    updated = store.update_intention(intention["id"], status="completed", acted=True)
    assert updated and updated["status"] == "completed"
    assert updated["last_acted_at"]

    reflection = store.add_reflection("review", "A concise reflection", confidence=0.8)
    assert store.recent_reflections(1)[0]["id"] == reflection["id"]

    decision = store.add_decision("silent", "Nothing useful now")
    assert store.recent_decisions(1)[0]["id"] == decision["id"]


def test_event_limit_is_enforced(tmp_path, config):
    limited = replace(config, database_path=str(tmp_path / "limited.db"), maximum_events=100)
    store = AgencyStore(limited)
    for number in range(130):
        store.add_event("test", summary=str(number))
    assert store.prune_events() == 30
    assert len(store.recent_events(200)) == 100


def test_encryption_fails_closed_without_key(tmp_path, config, monkeypatch):
    encrypted = replace(
        config,
        database_path=str(tmp_path / "encrypted.db"),
        database_encryption=True,
        database_key_env="TEST_AGENCY_KEY",
    )
    monkeypatch.delenv("TEST_AGENCY_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TEST_AGENCY_KEY"):
        AgencyStore(encrypted)


def test_invalid_intention_status_is_rejected(config):
    store = AgencyStore(config)
    item = store.add_intention("test")
    with pytest.raises(ValueError):
        store.update_intention(item["id"], status="invented")


def test_store_rejects_invalid_or_empty_ledger_records(config):
    store = AgencyStore(config)
    with pytest.raises(ValueError, match="title"):
        store.add_intention("  ")
    with pytest.raises(ValueError, match="autonomy"):
        store.add_intention("test", autonomy="unbounded")
    with pytest.raises(ValueError, match="status"):
        store.list_intentions("invented")
    with pytest.raises(ValueError, match="summary"):
        store.add_reflection("general", "  ")
    with pytest.raises(ValueError, match="message"):
        store.add_decision("speak", "A reason", message="")


def test_store_returns_the_sanitized_values_it_persists(config):
    store = AgencyStore(config)
    reflection = store.add_reflection("  ", "  useful  ", confidence=2)
    assert reflection["kind"] == "general"
    assert reflection["summary"] == "useful"
    assert reflection["confidence"] == 1.0
    decision = store.add_decision("silent", "  no value  ")
    assert decision["reason"] == "no value"


def test_newer_database_schema_fails_closed(config):
    store = AgencyStore(config)
    store.set_meta("schema_version", 999)
    with pytest.raises(RuntimeError, match="newer than supported"):
        AgencyStore(config)


def test_intention_due_dates_are_normalized_updated_cleared_and_validated(config):
    store = AgencyStore(config)
    intention = store.add_intention("Timed work", due_at="2026-07-16T07:00:00+02:00")
    assert intention["due_at"] == "2026-07-16T05:00:00+00:00"

    updated = store.update_intention(intention["id"], due_at="2026-07-17")
    assert updated["due_at"] == "2026-07-16T22:00:00+00:00"
    cleared = store.update_intention(intention["id"], due_at="")
    assert cleared["due_at"] is None

    with pytest.raises(ValueError, match="valid ISO-8601"):
        store.add_intention("Bad deadline", due_at="tomorrow-ish")
