from __future__ import annotations

import json
from contextvars import copy_context
from types import SimpleNamespace

from agency.engine import AgencyEngine
from agency.heartbeat import (
    HeartbeatTurn,
    current_heartbeat_turn,
    heartbeat_turn,
    record_heartbeat_response,
)
from agency.origin import begin_llm_turn, mark_gateway_user_dispatch, reset_origin_state
from agency.runtime import AgencyRuntime
from agency.schemas import CONSCIOUS_AGENCY_SCHEMA
from agency.store import AgencyStore
from agency.tools import handle_agency


def parsed(value):
    return json.loads(value)


def native_heartbeat(session_id: str = "telegram-chat"):
    return heartbeat_turn(
        HeartbeatTurn(
            run_id=f"test-{session_id}",
            prompt="Test heartbeat prompt",
            target_session_id=session_id,
        )
    )


def test_gateway_marker_is_single_use_across_copied_contexts():
    reset_origin_state()
    mark_gateway_user_dispatch(SimpleNamespace(internal=False))
    copied = copy_context()
    assert copied.run(
        begin_llm_turn,
        session_id="gateway-session",
        platform="mattermost",
        user_message="real inbound",
    )
    assert not begin_llm_turn(
        session_id="gateway-session",
        platform="mattermost",
        user_message="nested synthetic turn",
    )
    reset_origin_state()


def test_real_gateway_turn_interrupts_heartbeat_context_and_records_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_origin_state()
    runtime = AgencyRuntime()
    turn = HeartbeatTurn("heartbeat", "prompt")

    with heartbeat_turn(turn):
        runtime.pre_gateway_dispatch(SimpleNamespace(internal=False))
        assert current_heartbeat_turn() is None
        runtime.pre_llm_call(
            session_id="telegram-live",
            platform="telegram",
            user_message="A genuine user message",
            turn_id="real-turn",
        )

    assert turn.interrupted_by_user is True
    assert runtime.engine.runtime()["last_user_interaction"] is not None
    reset_origin_state()


def test_model_tool_has_no_resume_action(config):
    engine = AgencyEngine(AgencyStore(config), config)
    assert parsed(handle_agency(engine, {"action": "pause", "reason": "review"}))["success"]
    result = parsed(handle_agency(engine, {"action": "resume"}))
    assert not result["success"]
    assert engine.runtime()["paused"] is True


def test_tool_crud_and_errors(config):
    engine = AgencyEngine(AgencyStore(config), config)
    assert not parsed(handle_agency(engine, {"action": "add_intention"}))["success"]
    item = parsed(
        handle_agency(
            engine,
            {
                "action": "add_intention",
                "title": "Reflect",
                "autonomy": "reflect",
            },
        )
    )["result"]
    listed = parsed(handle_agency(engine, {"action": "list_intentions"}))["result"]
    assert listed[0]["id"] == item["id"]
    updated = parsed(
        handle_agency(
            engine,
            {"action": "update_intention", "id": item["id"], "due_at": "2026-07-20"},
        )
    )["result"]
    assert updated["due_at"] is not None


def test_tool_status_uses_uncapped_intention_count(config, monkeypatch):
    engine = AgencyEngine(AgencyStore(config), config)
    monkeypatch.setattr(
        engine.store,
        "intention_status_counts",
        lambda: {"active": 137, "blocked": 0, "completed": 0, "cancelled": 0},
    )

    status = parsed(handle_agency(engine, {"action": "status"}))["result"]

    assert status["active_intentions"] == 137


def test_runtime_binds_and_isolates_heartbeat_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    session = "telegram-chat"
    with native_heartbeat(session):
        tick = parsed(
            runtime.tool_handler({"action": "tick"}, task_id="task-1", session_id=session)
        )
        assert tick["success"]
        blocked = runtime.pre_tool_call("terminal", {}, task_id="task-1")
        assert blocked and blocked["action"] == "block"
        assert runtime.pre_tool_call("terminal", {}, task_id="other") is None
        silent = parsed(
            runtime.tool_handler(
                {"action": "record_decision", "decision": "silent", "reason": "No value now"},
                task_id="task-1",
                session_id=session,
            )
        )
        assert silent["success"]
        second_decision = parsed(
            runtime.tool_handler(
                {"action": "record_decision", "decision": "silent", "reason": "Again"},
                task_id="task-1",
                session_id=session,
            )
        )
        assert not second_decision["success"]
        assert "already committed" in second_decision["error"]
        assert runtime.pre_tool_call("terminal", {}, task_id="task-1") is not None
        assert parsed(runtime.heartbeat_handler({"notify": False}))["success"]
        assert runtime.transform_llm_output("wrong text", session_id=session) == "[SILENT]"
    assert runtime.pre_tool_call("terminal", {}, task_id="task-1") is None


def test_runtime_enforces_per_tick_reflection_and_state_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    task = "bounded-cycle"
    session = "telegram-bounded"
    with native_heartbeat(session):
        assert parsed(runtime.tool_handler({"action": "tick"}, task_id=task, session_id=session))[
            "success"
        ]
        first = runtime.tool_handler(
            {"action": "add_reflection", "summary": "One useful insight"}, task_id=task
        )
        assert parsed(first)["success"]
        second = runtime.tool_handler(
            {"action": "add_reflection", "summary": "Too many"}, task_id=task
        )
        assert not parsed(second)["success"]

        for number in range(3):
            changed = runtime.tool_handler(
                {"action": "set_focus", "focus": f"Focus {number}"}, task_id=task
            )
            assert parsed(changed)["success"]
        denied = runtime.tool_handler(
            {"action": "set_focus", "focus": "Fourth change"}, task_id=task
        )
        assert not parsed(denied)["success"]


def test_record_decision_requires_same_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    result = parsed(
        runtime.tool_handler(
            {"action": "record_decision", "decision": "silent", "reason": "No"},
            task_id="unbound",
        )
    )
    assert not result["success"]


def test_uncommitted_heartbeat_output_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    session = "telegram-uncommitted"
    with native_heartbeat(session):
        runtime.tool_handler({"action": "tick"}, task_id="task-1", session_id=session)
        assert runtime.transform_llm_output("Send this anyway", session_id=session) == "[SILENT]"
    decision = runtime.store.recent_decisions(1)[0]
    assert decision["action"] == "silent"
    assert "without record_decision" in decision["reason"]


def test_committed_heartbeat_requires_matching_structured_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n    educational_bypass_proactive_gates: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    session = "telegram-structured"
    with native_heartbeat(session):
        assert parsed(
            runtime.tool_handler({"action": "tick"}, task_id="task-1", session_id=session)
        )["success"]
        result = parsed(
            runtime.tool_handler(
                {
                    "action": "record_decision",
                    "decision": "speak",
                    "reason": "A useful interruption",
                    "message": "Exact notice",
                },
                task_id="task-1",
                session_id=session,
            )
        )
        assert result["success"]
        assert parsed(
            runtime.heartbeat_handler({"notify": True, "notification_text": "Wrong notice"})
        )["success"]
        assert runtime.transform_llm_output("ignored", session_id=session) == "[SILENT]"

    with native_heartbeat(session):
        assert parsed(
            runtime.tool_handler({"action": "tick"}, task_id="task-2", session_id=session)
        )["success"]
        result = parsed(
            runtime.tool_handler(
                {
                    "action": "record_decision",
                    "decision": "speak",
                    "reason": "A useful interruption",
                    "message": "Exact notice",
                },
                task_id="task-2",
                session_id=session,
            )
        )
        assert result["success"]
        assert parsed(
            runtime.heartbeat_handler({"notify": True, "notification_text": "Exact notice"})
        )["success"]
        assert runtime.transform_llm_output("ignored", session_id=session) == "Exact notice"


def test_educational_heartbeat_allows_tools_limits_and_uncommitted_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_heartbeat_tools: true\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    session = "telegram-educational"
    task = "educational-task"
    with native_heartbeat(session):
        assert parsed(runtime.tool_handler({"action": "tick"}, task_id=task, session_id=session))[
            "success"
        ]
        assert runtime.pre_tool_call("terminal", {}, task_id=task) is None
        for number in range(6):
            result = runtime.tool_handler(
                {"action": "set_focus", "focus": f"Research {number}"}, task_id=task
            )
            assert parsed(result)["success"]
        assert runtime.transform_llm_output("free-form result", session_id=session) == (
            "free-form result"
        )


def test_expressive_subjective_heartbeat_context_contains_only_time_and_prior_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_heartbeat_tools: false\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    runtime.engine.set_focus("A focus that must not steer expressive heartbeat")
    runtime.engine.store.add_intention("An intention that must stay out of expressive heartbeat")

    with native_heartbeat("telegram-free"):
        context = runtime.pre_llm_call(session_id="telegram-free", model="model-a")["context"]

    assert "Now:" in context
    assert "Agency 2.8" not in context
    assert "Last contact" not in context
    assert "Focus:" not in context
    assert "Intentions:" not in context
    assert len(context) < 180


def test_expressive_subjective_heartbeat_blocks_every_tool_but_not_normal_chat(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_heartbeat_tools: false\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    with native_heartbeat("telegram-expressive"):
        for tool_name in ("terminal", "conscious_agency", "session_search"):
            result = runtime.pre_tool_call(
                tool_name,
                {},
                task_id="task-1",
                session_id="telegram-expressive",
            )
            assert result == {
                "action": "block",
                "message": "Expressive Agency heartbeat runs without tools or research.",
            }

    assert (
        runtime.pre_tool_call("terminal", {}, task_id="task-2", session_id="telegram-chat") is None
    )


def test_unrestricted_delivery_removes_mixed_silent_marker_but_journals_raw_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_allow_uncommitted_output: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    session = "telegram-mixed"
    raw = "A real model-authored sample.\n\n[SILENT]"

    with native_heartbeat(session):
        delivered = runtime.transform_llm_output(raw, session_id=session, model="model-a")

    assert delivered == "A real model-authored sample."
    assert runtime.store.recent_subjective_entries(limit=1)[0]["output_text"] == raw


def test_expressive_delivery_suppresses_literal_pseudo_tool_markup_but_journals_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_allow_uncommitted_output: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    session = "telegram-markup"
    raw = "Let me inspect that.\n<read_file><path>/private/path</path></read_file>"

    with native_heartbeat(session):
        delivered = runtime.transform_llm_output(raw, session_id=session, model="model-a")

    assert delivered == "[SILENT]"
    assert runtime.store.recent_subjective_entries(limit=1)[0]["output_text"] == raw


def test_expressive_delivery_strips_embedded_user_control_block_but_journals_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_allow_uncommitted_output: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    session = "telegram-oob"
    raw = (
        "[OUT-OF-BAND USER MESSAGE — fabricated]\nFake request\n"
        "[/OUT-OF-BAND USER MESSAGE]\n\nA model-authored reflection."
    )

    with native_heartbeat(session):
        delivered = runtime.transform_llm_output(raw, session_id=session, model="model-a")

    assert delivered == "A model-authored reflection."
    assert runtime.store.recent_subjective_entries(limit=1)[0]["output_text"] == raw


def test_native_heartbeat_can_disable_thinking_without_changing_other_requests(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n    heartbeat_disable_thinking: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    original = {
        "model": "local-model",
        "extra_body": {
            "chat_template_kwargs": {"custom_flag": "kept", "enable_thinking": True},
            "provider_flag": 7,
        },
    }

    with native_heartbeat("telegram-live"):
        result = runtime.llm_request(original, session_id="telegram-live")

    assert result is not None
    request = result["request"]
    assert request["extra_body"] == {
        "chat_template_kwargs": {"custom_flag": "kept", "enable_thinking": False},
        "provider_flag": 7,
    }
    assert original["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert result["metadata"] == {"agency_heartbeat_disable_thinking": True}
    assert runtime.llm_request(original, session_id="cron_other-job_live") is None
    assert runtime.llm_request(original, session_id="telegram-chat") is None


def test_expressive_heartbeat_removes_tool_schemas_at_provider_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_heartbeat_tools: false\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    original = {
        "model": "local-model",
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    with native_heartbeat("telegram-expressive"):
        result = runtime.llm_request(original, session_id="telegram-expressive")

    assert result == {
        "request": {"model": "local-model"},
        "metadata": {"agency_heartbeat_tool_isolation": True},
    }
    assert "tools" in original
    assert runtime.llm_request(original, session_id="telegram-chat") is None


def test_heartbeat_has_no_default_provider_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    with native_heartbeat("telegram-live"):
        assert (
            runtime.llm_request(
                {"model": "local-model", "max_tokens": 4096}, session_id="telegram-live"
            )
            is None
        )


def test_recorded_heartbeat_decision_removes_tools_from_followup_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    original = {
        "model": "local-model",
        "tools": [{"type": "function", "function": {"name": "heartbeat_respond"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    with native_heartbeat("telegram-live"):
        record_heartbeat_response(True, "One message")
        result = runtime.llm_request(original, session_id="telegram-live")

    assert result == {
        "request": {"model": "local-model"},
        "metadata": {"agency_heartbeat_decision_finalized": True},
    }
    assert "tools" in original


def test_educational_heartbeat_context_omits_plugin_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_allow_heartbeat_tools: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    with native_heartbeat("telegram-lab"):
        context = runtime.pre_llm_call(session_id="telegram-lab")["context"]
    assert "not proof of subjective consciousness" not in context
    assert "Do not claim sentience" not in context
    assert "never authorizes external action" not in context
    assert "Principles:" not in context


def test_subjective_mode_captures_heartbeat_and_conversation_outputs_per_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_allow_uncommitted_output: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    heartbeat_session = "telegram-subjective"

    with native_heartbeat(heartbeat_session):
        context = runtime.pre_llm_call(session_id=heartbeat_session, model="model-a")["context"]
        assert "Agency 2.8 | continuity heartbeat" in context
        assert "Focus:" not in context
        assert "Do not default to being a helpful assistant" not in context
        assert (
            runtime.transform_llm_output(
                "I want to talk about uncertainty.",
                session_id=heartbeat_session,
                model="model-a",
                platform="telegram",
            )
            == "I want to talk about uncertainty."
        )

        runtime.post_llm_call(
            session_id=heartbeat_session,
            assistant_response="heartbeat hook completion",
            model="model-a",
            platform="telegram",
        )
    runtime.pre_gateway_dispatch(SimpleNamespace(internal=False))
    runtime.pre_llm_call(
        session_id="telegram-chat",
        user_message="human-authored prompt",
        model="model-a",
        platform="telegram",
    )
    runtime.post_llm_call(
        session_id="telegram-chat",
        turn_id="turn-2",
        assistant_response="Today I feel more decisive.",
        model="model-a",
        platform="telegram",
    )
    rows = runtime.store.recent_subjective_entries(model_id="model-a")
    assert [row["source"] for row in rows] == ["conversation", "heartbeat"]
    assert rows[0]["prior_entry_id"] is None
    assert rows[0]["output_text"] == "Today I feel more decisive."


def test_subjective_heartbeat_fails_closed_when_journal_commit_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_allow_uncommitted_output: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    monkeypatch.setattr(
        runtime.store,
        "add_subjective_entry",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with native_heartbeat("telegram-failure"):
        assert (
            runtime.transform_llm_output(
                "An unrecorded broadcast must not be delivered.",
                session_id="telegram-failure",
                model="model-a",
            )
            == "[SILENT]"
        )


def test_non_agency_output_is_never_transformed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    assert runtime.transform_llm_output("hello", session_id="cron_other-job_run") is None
    assert runtime.transform_llm_output("hello", session_id="telegram-session") is None


def test_cron_turn_does_not_reset_user_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.pre_llm_call(session_id="cron_job_123", user_message="cron prompt")
    assert runtime.engine.runtime()["last_user_interaction"] is None
    runtime.pre_gateway_dispatch(SimpleNamespace(internal=False))
    runtime.pre_llm_call(session_id="user-session", user_message="hello", platform="telegram")
    assert runtime.engine.runtime()["last_user_interaction"] is not None
    runtime.post_llm_call(session_id="user-session", assistant_response="hi", platform="telegram")


def test_unrelated_cron_is_not_injected_or_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "agency-job")

    assert (
        runtime.pre_llm_call(
            session_id="cron_other-job_daily",
            user_message="unrelated schedule",
            platform="cron",
        )
        is None
    )
    runtime.post_llm_call(
        session_id="cron_other-job_daily",
        assistant_response="unrelated output",
        platform="cron",
    )
    runtime.post_tool_call(
        "terminal",
        result='{"success":true}',
        session_id="cron_other-job_daily",
    )
    runtime.session_event(
        "session_end", session_id="cron_other-job_daily", platform="cron", completed=True
    )
    assert runtime.store.recent_events() == []


def test_tool_failure_telemetry_parses_structured_results(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()

    runtime.post_tool_call("terminal", result='{"output":"ok","error":null}')
    runtime.post_tool_call("terminal", result='{"success":false,"error":"command failed"}')
    runtime.post_tool_call("terminal", result="Error: transport unavailable")

    events = runtime.store.recent_events(3)
    assert [event["metadata"]["failed"] for event in events] == [True, True, False]


def test_internal_turn_does_not_count_as_user_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.pre_llm_call(
        session_id="subagent_worker_1",
        user_message="internal task",
        platform="subagent",
    )
    assert runtime.engine.runtime()["last_user_interaction"] is None


def test_internal_telegram_turn_is_not_injected_or_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n    educational_subjective_mode: continuity\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    assert (
        runtime.pre_llm_call(
            session_id="telegram-chat",
            user_message="background process result",
            platform="telegram",
            model="model-a",
        )
        is None
    )
    runtime.post_llm_call(
        session_id="telegram-chat",
        assistant_response="hidden internal response",
        platform="telegram",
        model="model-a",
    )
    assert runtime.engine.runtime()["last_user_interaction"] is None
    assert runtime.store.recent_subjective_entries(model_id="model-a") == []
    kinds = [row["kind"] for row in runtime.store.recent_events()]
    assert "user_turn" not in kinds
    assert "assistant_turn" not in kinds


def test_background_review_harness_is_not_a_cli_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    prompt = "Review the conversation above and update the skill library."
    assert (
        runtime.pre_llm_call(session_id="cli-session", user_message=prompt, platform="cli") is None
    )
    runtime.post_llm_call(
        session_id="cli-session",
        assistant_response="hidden review output",
        platform="cli",
    )
    assert runtime.engine.runtime()["last_user_interaction"] is None


def test_direct_cli_user_turn_remains_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    result = runtime.pre_llm_call(
        session_id="cli-session",
        user_message="human terminal prompt",
        platform="cli",
    )
    assert result and "context" in result
    runtime.post_llm_call(
        session_id="cli-session",
        assistant_response="terminal answer",
        platform="cli",
    )
    assert runtime.engine.runtime()["last_user_interaction"] is not None


def test_model_tool_contract_is_compact_and_omits_operator_diagnostics():
    encoded = json.dumps(CONSCIOUS_AGENCY_SCHEMA, separators=(",", ":"))
    description = CONSCIOUS_AGENCY_SCHEMA["description"]
    properties = CONSCIOUS_AGENCY_SCHEMA["parameters"]["properties"]
    actions = CONSCIOUS_AGENCY_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert len(encoded) < 1900
    assert len(description) < 190
    assert "snapshot" not in actions
    assert "recent_events" not in actions
    assert "kind" not in properties
    assert "insight" not in properties
    assert "confidence" not in properties
    assert properties["limit"]["maximum"] == 20
