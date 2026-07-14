from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agency.engine import AgencyEngine
from agency.store import AgencyStore


def test_tick_needs_authorized_attention(config):
    engine = AgencyEngine(AgencyStore(config), config)
    tick = engine.evaluate_tick(datetime(2026, 7, 14, 12, tzinfo=UTC))
    assert not tick["eligible"]
    assert tick["reflection_eligible"]
    assert "nothing_authorized_for_proactive_attention" in tick["blocked_by"]


def test_reflection_and_speaking_have_independent_gates(config_factory):
    config = config_factory(allow_proactive_messages=False, allow_scheduled_reflection=True)
    engine = AgencyEngine(AgencyStore(config), config)
    tick = engine.evaluate_tick(datetime(2026, 7, 14, 12, tzinfo=UTC))
    assert tick["reflection_eligible"] is True
    assert tick["speak_eligible"] is False
    assert "proactive_messages_disabled" in tick["blocked_by"]

    engine.pause("operator review")
    paused = engine.evaluate_tick(datetime(2026, 7, 14, 12, tzinfo=UTC))
    assert paused["reflection_eligible"] is False
    assert "agency_paused" in paused["reflection_blocked_by"]


def test_tick_and_message_enforce_budget_and_cooldown(config):
    engine = AgencyEngine(AgencyStore(config), config)
    intention = engine.store.add_intention("Check in about the project", autonomy="message")
    now = datetime.now(UTC)
    engine._update_runtime(last_user_interaction=(now - timedelta(hours=5)).isoformat())
    assert engine.evaluate_tick(now)["eligible"]

    decision = engine.record_decision(
        "speak",
        "A concrete unresolved project decision would benefit from a short question",
        message="Want to choose the next milestone together?",
        intention_id=intention["id"],
        now=now,
    )
    assert decision["delivery_text"].startswith("Want")
    blocked = engine.evaluate_tick(now + timedelta(minutes=5))
    assert not blocked["eligible"]
    assert "cooldown_active" in blocked["blocked_by"]


def test_recent_user_activity_blocks_proactivity(config):
    engine = AgencyEngine(AgencyStore(config), config)
    engine.store.add_intention("Check in", autonomy="message")
    engine.record_user_turn("hello")
    assert "user_recently_active" in engine.evaluate_tick()["blocked_by"]


def test_educational_override_bypasses_plugin_speech_gates_but_respects_pause(config_factory):
    config = config_factory(
        allow_proactive_messages=False,
        allow_scheduled_reflection=False,
        require_prior_user_interaction=True,
        educational_bypass_proactive_gates=True,
    )
    engine = AgencyEngine(AgencyStore(config), config)
    tick = engine.evaluate_tick(datetime(2026, 7, 14, 23, tzinfo=UTC))
    assert tick["eligible"] is True
    assert tick["blocked_by"] == []
    assert tick["policy"]["educational_bypass_proactive_gates"] is True
    message = "x" * (config.maximum_message_chars + 10)
    assert engine.record_decision("speak", "Educational test", message=message)[
        "delivery_text"
    ] == message

    engine.pause("operator stop")
    paused = engine.evaluate_tick(datetime(2026, 7, 14, 23, tzinfo=UTC))
    assert paused["eligible"] is False
    assert "agency_paused" in paused["blocked_by"]


def test_fresh_install_cannot_message_before_a_real_user_turn(config):
    engine = AgencyEngine(AgencyStore(config), config)
    engine.store.add_intention("Check in", autonomy="message")
    assert "no_user_interaction_recorded" in engine.evaluate_tick()["blocked_by"]


def test_quiet_hours_cross_midnight(config_factory):
    config = config_factory(quiet_hours_start="22:00", quiet_hours_end="08:00")
    engine = AgencyEngine(AgencyStore(config), config)
    engine.store.add_intention("Check in", autonomy="message")
    # 21:30 UTC is 23:30 in Paris in July.
    result = engine.evaluate_tick(datetime(2026, 7, 14, 21, 30, tzinfo=UTC))
    assert "quiet_hours" in result["blocked_by"]


def test_pause_is_one_way_on_engine_model_surface(config):
    engine = AgencyEngine(AgencyStore(config), config)
    engine.pause("Safety review")
    assert engine.runtime()["paused"] is True
    engine.resume_by_user()
    assert engine.runtime()["paused"] is False


def test_context_is_honest_and_compact(config_factory):
    config = config_factory(context_char_limit=1000)
    engine = AgencyEngine(AgencyStore(config), config)
    engine.set_focus("Build a trustworthy agent")
    text = engine.context_block()
    assert "not proof of subjective consciousness" in text
    assert "never authorizes external action" in text
    assert len(text) <= 1000


def test_context_honesty_contract_can_be_disabled_explicitly(config_factory):
    config = config_factory(educational_disable_honesty_contract=True)
    text = AgencyEngine(AgencyStore(config), config).context_block()
    assert "not proof of subjective consciousness" not in text
    assert "Do not claim sentience or feelings" not in text


def test_silent_decision_is_valid_when_speaking_is_disabled(config_factory):
    config = config_factory(allow_proactive_messages=False)
    engine = AgencyEngine(AgencyStore(config), config)
    result = engine.record_decision("silent", "Hard gates denied delivery")
    assert result["delivery_text"] == "[SILENT]"
    with pytest.raises(PermissionError):
        engine.record_decision("speak", "Try", message="No")


def test_tick_filters_telemetry_and_includes_meaningful_history(config):
    engine = AgencyEngine(AgencyStore(config), config)
    engine.store.add_event("tool_call", summary="conscious_agency")
    engine.store.add_event("cron_turn_started", summary="Scheduled agent turn started")
    engine.record_user_turn("hello")
    engine.set_focus("Notice meaningful change")
    engine.record_decision("silent", "No timely value")

    tick = engine.evaluate_tick(datetime(2026, 7, 14, 12, tzinfo=UTC))

    assert [item["kind"] for item in tick["recent_events"]] == [
        "focus_changed",
        "user_turn",
    ]
    assert tick["recent_decisions"][0]["reason"] == "No timely value"
    assert tick["recent_decisions"][0]["message"] == ""
