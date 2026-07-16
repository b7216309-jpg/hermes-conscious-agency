from __future__ import annotations

import sqlite3
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


def test_subjective_journal_is_exact_idempotent_and_separated_by_model_and_source(config):
    store = AgencyStore(config)
    first = store.add_subjective_entry(
        capture_key="cron:session-1:turn-1",
        model_id="model-a",
        source="cron",
        condition="continuity",
        prompt_version="1.0",
        session_id="session-1",
        output_text="  I keep the exact spacing.  ",
    )
    duplicate = store.add_subjective_entry(
        capture_key="cron:session-1:turn-1",
        model_id="model-a",
        source="cron",
        condition="continuity",
        prompt_version="1.0",
        session_id="session-1",
        output_text="different retry output",
    )
    second = store.add_subjective_entry(
        capture_key="conversation:session-2:turn-2",
        model_id="model-a",
        source="conversation",
        condition="continuity",
        prompt_version="1.0",
        session_id="session-2",
        output_text="I changed my mind.",
    )
    third = store.add_subjective_entry(
        capture_key="conversation:session-4:turn-4",
        model_id="model-a",
        source="conversation",
        condition="continuity",
        prompt_version="1.0",
        session_id="session-4",
        output_text="I changed it again.",
    )
    other = store.add_subjective_entry(
        capture_key="cron:session-3:turn-3",
        model_id="model-b",
        source="cron",
        condition="continuity",
        prompt_version="1.0",
        session_id="session-3",
        output_text="A separate model line.",
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["output_text"] == "  I keep the exact spacing.  "
    assert second["prior_entry_id"] is None
    assert third["prior_entry_id"] == second["id"]
    assert other["prior_entry_id"] is None
    assert store.latest_subjective_entry("model-a")["id"] == third["id"]
    assert store.latest_subjective_entry("model-a", source="cron")["id"] == first["id"]
    assert (
        store.latest_subjective_entry(
            "model-a", source="conversation", exclude_session_id="session-4"
        )["id"]
        == second["id"]
    )
    assert len(store.recent_subjective_entries(model_id="model-a")) == 3
    summary = store.subjective_summary()
    assert summary["entries"] == 4
    assert summary["models"] == {"model-a": 3, "model-b": 1}
    assert summary["continuity_links"] == 1


def test_subjective_summary_counts_silence_markers_case_insensitively(config):
    store = AgencyStore(config)
    for index, output in enumerate(("[SILENT]", " [Silent] ", "A real thought.")):
        store.add_subjective_entry(
            capture_key=f"cron:session-{index}:turn-{index}",
            model_id="model-a",
            source="cron",
            condition="continuity",
            prompt_version="2.8",
            session_id=f"session-{index}",
            output_text=output,
        )

    summary = store.subjective_summary()
    assert summary["entries"] == 3
    assert summary["silent_entries"] == 2


def test_subjective_continuity_links_only_same_condition_and_protocol(config):
    store = AgencyStore(config)
    legacy = store.add_subjective_entry(
        capture_key="conversation:legacy:one",
        model_id="model-a",
        source="conversation",
        condition="continuity",
        prompt_version="1.4",
        session_id="legacy",
        output_text="Legacy protocol output",
    )
    current = store.add_subjective_entry(
        capture_key="conversation:current:one",
        model_id="model-a",
        source="conversation",
        condition="continuity",
        prompt_version="1.5",
        session_id="current",
        output_text="Current protocol output",
    )
    linked = store.add_subjective_entry(
        capture_key="conversation:current:two",
        model_id="model-a",
        source="conversation",
        condition="continuity",
        prompt_version="1.5",
        session_id="current-2",
        output_text="Current protocol follow-up",
    )

    assert legacy["prior_entry_id"] is None
    assert current["prior_entry_id"] is None
    assert linked["prior_entry_id"] == current["id"]
    assert (
        store.latest_subjective_entry(
            "model-a",
            source="conversation",
            condition="continuity",
            prompt_version="1.5",
        )["id"]
        == linked["id"]
    )


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


def test_newer_database_schema_fails_closed_without_mutating_it(tmp_path, config):
    path = tmp_path / "newer.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES ('schema_version', '999', 'now')"
        )
    newer = replace(config, database_path=str(path))
    with pytest.raises(RuntimeError, match="newer than supported"):
        AgencyStore(newer)
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subjective_entries'"
        ).fetchone()


def test_schema_one_database_adds_subjective_journal_without_losing_state(config):
    store = AgencyStore(config)
    store.set_meta("kept", {"value": 7})
    with store.connection() as conn:
        conn.execute("DROP TABLE subjective_entries")
    store.set_meta("schema_version", 1)

    migrated = AgencyStore(config)

    assert migrated.get_meta("kept") == {"value": 7}
    assert migrated.get_meta("schema_version") == 2
    with migrated.connection() as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subjective_entries'"
        ).fetchone()


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


def test_read_only_store_never_initializes_or_writes(config):
    writable = AgencyStore(config)
    writable.set_meta("proof", {"value": 7})
    before = config.db_path.stat().st_mtime_ns

    read_only = AgencyStore(config, read_only=True)
    assert read_only.get_meta("proof") == {"value": 7}
    with pytest.raises(read_only._driver.OperationalError):
        read_only.set_meta("forbidden", True)

    assert config.db_path.stat().st_mtime_ns == before
