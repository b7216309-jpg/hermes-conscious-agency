from __future__ import annotations

import json

from agency.engine import AgencyEngine
from agency.runtime import AgencyRuntime
from agency.schemas import CONSCIOUS_AGENCY_SCHEMA
from agency.store import AgencyStore
from agency.tools import handle_agency


def parsed(value):
    return json.loads(value)


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


def test_runtime_binds_and_isolates_proactive_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    session = "cron_job-1_20260714"
    tick = parsed(runtime.tool_handler({"action": "tick"}, task_id="task-1", session_id=session))
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
    assert runtime.transform_llm_output("wrong text", session_id=session) == "[SILENT]"
    assert runtime.pre_tool_call("terminal", {}, task_id="task-1") is None


def test_runtime_enforces_per_tick_reflection_and_state_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    task = "bounded-cycle"
    session = "cron_job-1_bounded"
    assert parsed(runtime.tool_handler({"action": "tick"}, task_id=task, session_id=session))[
        "success"
    ]
    first = runtime.tool_handler(
        {"action": "add_reflection", "summary": "One useful insight"}, task_id=task
    )
    assert parsed(first)["success"]
    second = runtime.tool_handler({"action": "add_reflection", "summary": "Too many"}, task_id=task)
    assert not parsed(second)["success"]

    for number in range(3):
        changed = runtime.tool_handler(
            {"action": "set_focus", "focus": f"Focus {number}"}, task_id=task
        )
        assert parsed(changed)["success"]
    denied = runtime.tool_handler({"action": "set_focus", "focus": "Fourth change"}, task_id=task)
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


def test_uncommitted_cron_output_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    session = "cron_job-1_uncommitted"
    runtime.tool_handler({"action": "tick"}, task_id="task-1", session_id=session)

    assert runtime.transform_llm_output("Send this anyway", session_id=session) == "[SILENT]"
    decision = runtime.store.recent_decisions(1)[0]
    assert decision["action"] == "silent"
    assert "without record_decision" in decision["reason"]


def test_educational_cron_allows_tools_limits_and_uncommitted_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_cron_tools: true\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    session = "cron_job-1_educational"
    task = "educational-task"
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


def test_educational_cron_context_omits_plugin_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_allow_cron_tools: true\n",
        encoding="utf-8",
    )
    runtime = AgencyRuntime()
    runtime.store.set_meta("cron_job_id", "job-1")
    context = runtime.pre_llm_call(session_id="cron_job-1_lab")["context"]
    assert "not proof of subjective consciousness" not in context
    assert "Do not claim sentience" not in context
    assert "never authorizes external action" not in context
    assert "Principles:" not in context


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
    runtime.pre_llm_call(session_id="user-session", user_message="hello", platform="telegram")
    assert runtime.engine.runtime()["last_user_interaction"] is not None


def test_internal_turn_does_not_count_as_user_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = AgencyRuntime()
    runtime.pre_llm_call(
        session_id="subagent_worker_1",
        user_message="internal task",
        platform="subagent",
    )
    assert runtime.engine.runtime()["last_user_interaction"] is None


def test_tool_contract_explains_action_specific_requirements():
    description = CONSCIOUS_AGENCY_SCHEMA["description"]
    properties = CONSCIOUS_AGENCY_SCHEMA["parameters"]["properties"]
    assert "add_reflection requires summary" in description
    assert "record_decision requires decision and reason" in description
    assert "Required for add_reflection" in properties["summary"]["description"]
