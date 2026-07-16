from __future__ import annotations

import asyncio
import json
import sys
from contextvars import Context, ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agency.config import AgencyConfig
from agency.heartbeat import (
    HEARTBEAT_OK,
    HEARTBEAT_TRANSCRIPT_PROMPT,
    HeartbeatRunner,
    HeartbeatTask,
    HeartbeatTurn,
    _active_at,
    _migrate_legacy_config,
    _patch_gateway,
    current_heartbeat_turn,
    heartbeat_content_effectively_empty,
    heartbeat_phase_seconds,
    is_task_due,
    next_phase_due,
    parse_heartbeat_tasks,
    record_heartbeat_response,
    release_heartbeat_for_user_turn,
    remove_legacy_cron,
    request_heartbeat_wake,
    should_defer_wake,
    strip_heartbeat_ack,
)
from agency.store import AgencyStore


def test_comment_and_header_only_heartbeat_is_effectively_empty():
    assert heartbeat_content_effectively_empty(
        "# HEARTBEAT.md\n<!-- comments may span\nseveral lines -->\n- [ ]\n```text\n```\n"
    )
    assert not heartbeat_content_effectively_empty("# Heartbeat\nUse this wake as your own turn.")
    assert not heartbeat_content_effectively_empty(None)


def test_parses_valid_tasks_and_ignores_invalid_entries():
    content = """
# Directives
Use this wake freely.

tasks:
  - name: observe
    interval: 45m
    prompt: Notice whether anything changed.
  - name: invalid
    interval: whenever
    prompt: This entry is ignored.

After: preserved
"""
    assert parse_heartbeat_tasks(content) == [
        HeartbeatTask("observe", "45m", "Notice whether anything changed.")
    ]
    assert is_task_due(None, "45m", 1000)
    assert not is_task_due(900, "45m", 1000)
    assert is_task_due(100, "1s", 1000)


def test_scheduler_phase_is_stable_and_future_due_is_strict():
    first = heartbeat_phase_seconds("machine", "agency", 1800)
    assert first == heartbeat_phase_seconds("machine", "agency", 1800)
    assert 0 <= first < 1800
    due = next_phase_due(10_000, 1800, first)
    assert due > 10_000
    assert due <= 11_800


def test_active_hours_support_daytime_overnight_and_closed_window():
    daytime = AgencyConfig(
        timezone="UTC", heartbeat_active_hours_start="08:00", heartbeat_active_hours_end="22:00"
    ).validate()
    overnight = AgencyConfig(
        timezone="UTC", heartbeat_active_hours_start="22:00", heartbeat_active_hours_end="08:00"
    ).validate()
    closed = AgencyConfig(
        timezone="UTC", heartbeat_active_hours_start="00:00", heartbeat_active_hours_end="00:00"
    ).validate()
    full_day = AgencyConfig(
        timezone="UTC", heartbeat_active_hours_start="00:00", heartbeat_active_hours_end="24:00"
    ).validate()
    noon = datetime(2026, 7, 16, 12, tzinfo=UTC).timestamp()
    midnight = datetime(2026, 7, 16, 0, tzinfo=UTC).timestamp()
    assert _active_at(noon, daytime)
    assert not _active_at(midnight, daytime)
    assert not _active_at(noon, overnight)
    assert _active_at(midnight, overnight)
    assert not _active_at(noon, closed)
    assert _active_at(noon, full_day)


@pytest.mark.parametrize(
    ("raw", "silent", "visible"),
    [
        (HEARTBEAT_OK, True, ""),
        (f"<b>{HEARTBEAT_OK}</b>", True, ""),
        (f"{HEARTBEAT_OK} Nothing urgent.", True, ""),
        (f"A real alert. {HEARTBEAT_OK}", True, ""),
        (
            f"Text {HEARTBEAT_OK} in the middle stays visible",
            False,
            f"Text {HEARTBEAT_OK} in the middle stays visible",
        ),
        ("A real alert.", False, "A real alert."),
    ],
)
def test_acknowledgement_contract(raw, silent, visible):
    assert strip_heartbeat_ack(raw, 300) == (silent, visible)


def test_long_text_next_to_ack_is_delivered():
    payload = "x" * 301
    assert strip_heartbeat_ack(f"{HEARTBEAT_OK} {payload}", 300) == (False, payload)


def test_cooldown_priorities_and_flood_guard():
    common = dict(now=100, next_due=90, last_started=95, recent_starts=[96, 97])
    assert should_defer_wake(intent="manual", **common, flood_threshold=2) == ""
    assert should_defer_wake(intent="immediate", **common, flood_threshold=2) == "flood"
    assert should_defer_wake(intent="scheduled", **common, flood_threshold=5) == ""
    assert (
        should_defer_wake(
            intent="scheduled", now=80, next_due=90, last_started=None, recent_starts=[]
        )
        == "not_due"
    )
    assert (
        should_defer_wake(intent="event", now=100, next_due=90, last_started=10, recent_starts=[])
        == ""
    )


def test_wake_file_is_atomic_and_last_request_coalesces(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = request_heartbeat_wake("event", "first")
    second = request_heartbeat_wake("manual", "second")
    path = tmp_path / "conscious-agency" / "heartbeat-wake.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert first != second
    assert payload["request_id"] == second
    assert payload["intent"] == "manual"
    assert not path.with_suffix(".tmp").exists()


def test_heartbeat_response_is_scoped_and_validated():
    with pytest.raises(PermissionError):
        record_heartbeat_response(False)
    turn = HeartbeatTurn("run", "prompt")
    from agency.heartbeat import heartbeat_turn

    with heartbeat_turn(turn):
        with pytest.raises(ValueError):
            record_heartbeat_response(True, "")
        first = record_heartbeat_response(False)
        second = record_heartbeat_response(True, "must not replace the first decision")
        assert first["notify"] is False
        assert first["accepted"] is True
        assert "End the turn" in first["instruction"]
        assert second["notify"] is False
        assert second["notification_text"] == ""
        assert second["accepted"] is False


def test_migration_removes_only_recorded_legacy_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = AgencyConfig(database_path=str(tmp_path / "agency.db"))
    AgencyStore(config).set_meta("cron_job_id", "agency-only-job")
    monkeypatch.setattr("agency.heartbeat.load_config", lambda: config)
    calls = []
    monkeypatch.setattr("agency.heartbeat.shutil.which", lambda name: "/usr/bin/hermes")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="removed", stderr="")

    monkeypatch.setattr("agency.heartbeat.subprocess.run", fake_run)
    gate = tmp_path / "scripts" / "conscious_agency_gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text("legacy", encoding="utf-8")

    result = remove_legacy_cron()

    assert calls[0][0] == ["/usr/bin/hermes", "cron", "remove", "agency-only-job"]
    assert result["removed"] is True
    assert not gate.exists()
    assert AgencyStore(config).get_meta("cron_job_id", "missing") == ""


def test_migration_atomically_rewrites_only_agency_legacy_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugins:\n"
        "  unrelated-plugin:\n"
        "    cron_schedule: every 5m\n"
        "  conscious-agency:\n"
        "    allow_scheduled_reflection: true\n"
        "    cron_schedule: every 1h\n"
        "    cron_delivery: origin\n"
        "    cron_disable_thinking: true\n"
        "    educational_allow_cron_tools: false\n",
        encoding="utf-8",
    )

    result = _migrate_legacy_config()

    assert result["changed"] is True
    assert result["backup"] and Path(result["backup"]).is_file()
    migrated = path.read_text(encoding="utf-8")
    assert "heartbeat_enabled: true" in migrated
    assert "heartbeat_every: 1h" in migrated
    assert "heartbeat_target: last" in migrated
    assert "heartbeat_disable_thinking: true" in migrated
    assert "educational_allow_heartbeat_tools: false" in migrated
    assert "unrelated-plugin:\n    cron_schedule: every 5m" in migrated
    agency_section = migrated.split("conscious-agency:", 1)[1]
    assert "cron_schedule" not in agency_section


@dataclass
class FakeSource:
    platform: object
    chat_id: str
    thread_id: str | None = None


@dataclass
class FakeEntry:
    session_id: str
    updated_at: datetime
    origin: FakeSource | None


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return SimpleNamespace(success=True)


class FakeAsyncStore:
    def __init__(self):
        self.saves = 0

    async def _save(self):
        self.saves += 1


class FakeGateway:
    def __init__(self, entries, response="A native heartbeat message"):
        self.entries = entries
        self.response = response
        self.events = []
        self.prompts = []
        self.adapter = FakeAdapter()
        self.session_store = SimpleNamespace(list_sessions=lambda: entries)
        self.async_session_store = FakeAsyncStore()

    def _adapter_for_source(self, source):
        return self.adapter

    async def _handle_message(self, event):
        self.events.append(event)
        turn = current_heartbeat_turn()
        self.prompts.append(turn.prompt if turn else "")
        self.entries[0].updated_at = datetime.now(UTC)
        return self.response


def _install_fake_gateway_module(monkeypatch):
    gateway_module = ModuleType("gateway")
    gateway_module.__path__ = []
    platforms_module = ModuleType("gateway.platforms")
    base_module = ModuleType("gateway.platforms.base")

    @dataclass
    class MessageEvent:
        text: str
        source: object
        internal: bool = False
        metadata: dict | None = None

    base_module.MessageEvent = MessageEvent
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_module)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base_module)


def test_gateway_patch_supports_runtime_reload_display_buffering_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gateway_module = ModuleType("gateway")
    gateway_module.__path__ = []
    run_module = ModuleType("gateway.run")
    display_module = ModuleType("gateway.display_config")
    display_module.resolve_display_setting = lambda config, platform, name: f"normal:{name}"

    class GatewayRunner:
        def __init__(self):
            self.finished = 0

        async def _finish_startup_restore(self):
            self.finished += 1
            return "ready"

    run_module.GatewayRunner = GatewayRunner
    run_module._current_max_iterations = lambda: 90
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.run", run_module)
    monkeypatch.setitem(sys.modules, "gateway.display_config", display_module)
    first = SimpleNamespace(calls=[], ensure_heartbeat=lambda gateway: first.calls.append(gateway))
    second = SimpleNamespace(
        calls=[], ensure_heartbeat=lambda gateway: second.calls.append(gateway)
    )

    assert _patch_gateway(first)
    assert _patch_gateway(second)
    gateway = GatewayRunner()
    assert asyncio.run(gateway._finish_startup_restore()) == "ready"
    assert first.calls == []
    assert second.calls == [gateway]
    assert display_module.resolve_display_setting(None, "telegram", "streaming") == (
        "normal:streaming"
    )
    from agency.heartbeat import heartbeat_turn

    with heartbeat_turn(HeartbeatTurn("run", "prompt")):
        assert display_module.resolve_display_setting(None, "telegram", "streaming") is False
        assert display_module.resolve_display_setting(None, "telegram", "unrelated") == (
            "normal:unrelated"
        )
        assert run_module._current_max_iterations() == 8
    assert run_module._current_max_iterations() == 90


def test_runner_reconciles_interrupted_process_run(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    store.set_meta(
        "heartbeat_state",
        {
            "last_started_at": 20.0,
            "last_completed_at": 10.0,
            "last_status": "silent",
            "runs": 3,
        },
    )
    runner = HeartbeatRunner(SimpleNamespace(store=store))

    runner._reconcile_interrupted_run()
    runner._reconcile_interrupted_run()

    state = store.get_meta("heartbeat_state", {})
    assert state["last_status"] == "interrupted"
    assert state["last_reason"] == "gateway_restart"
    assert state["runs"] == 3
    events = [item for item in store.recent_events(10) if item["kind"] == "heartbeat_interrupted"]
    assert len(events) == 1


def test_runner_task_starts_with_clean_context(tmp_path, config_factory):
    marker = ContextVar("inbound_marker", default="clean")
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    observed = []

    async def fake_run(gateway):
        observed.append(marker.get())

    runner.run = fake_run

    class Gateway:
        pass

    async def scenario():
        marker.set("copied-user-marker")
        task = runner.start(Gateway())
        assert task is not None
        await task

    asyncio.run(scenario())
    assert observed == ["clean"]


def test_run_once_uses_latest_external_session_buffers_and_restores_activity(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="last")
    store = AgencyStore(config)
    runtime = SimpleNamespace(store=store)
    runner = HeartbeatRunner(runtime)
    original_time = datetime(2026, 7, 15, tzinfo=UTC)
    external = FakeEntry(
        "telegram-main", original_time, FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    )
    local = FakeEntry("local", datetime.now(UTC), None)
    gateway = FakeGateway([external, local])
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}

    executed = asyncio.run(runner.run_once(gateway, config, state, reason="manual test"))

    assert executed is True
    assert gateway.events[0].text == HEARTBEAT_TRANSCRIPT_PROMPT
    assert gateway.events[0].internal is True
    assert gateway.events[0].metadata["gateway_session_id"] == "telegram-main"
    assert gateway.adapter.sent == [("chat-1", "A native heartbeat message", None)]
    assert external.updated_at == original_time
    assert gateway.async_session_store.saves == 1
    assert state["last_started_at"] > 0
    assert store.get_meta("heartbeat_state", {})["last_status"] == "delivered"


def test_run_once_target_none_executes_without_delivery(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="none")
    runtime = SimpleNamespace(store=AgencyStore(config))
    runner = HeartbeatRunner(runtime)
    entry = FakeEntry(
        "telegram-main",
        datetime(2026, 7, 15, tzinfo=UTC),
        FakeSource(SimpleNamespace(value="telegram"), "chat-1"),
    )
    gateway = FakeGateway([entry])
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}

    assert asyncio.run(runner.run_once(gateway, config, state, reason="scheduled")) is True
    assert gateway.events
    assert gateway.adapter.sent == []


def test_real_user_interrupt_clears_heartbeat_delivery_and_preserves_activity(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="last")
    store = AgencyStore(config)
    transformed = []
    runtime = SimpleNamespace(
        store=store,
        transform_llm_output=lambda value, **kwargs: transformed.append(value) or value,
    )
    runner = HeartbeatRunner(runtime)
    original_time = datetime(2026, 7, 15, tzinfo=UTC)
    user_time = datetime(2026, 7, 16, tzinfo=UTC)
    entry = FakeEntry(
        "telegram-main",
        original_time,
        FakeSource(SimpleNamespace(value="telegram"), "chat-1"),
    )

    class InterruptGateway(FakeGateway):
        async def _handle_message(self, event):
            entry.updated_at = user_time
            interrupted = Context().run(
                release_heartbeat_for_user_turn,
                SimpleNamespace(
                    internal=False,
                    text="a real user message",
                    source=entry.origin,
                ),
            )
            assert interrupted is not None
            return "the real user's assistant response"

    gateway = InterruptGateway([entry])
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}

    assert asyncio.run(runner.run_once(gateway, config, state, reason="scheduled")) is True
    assert gateway.adapter.sent == []
    assert transformed == []
    assert entry.updated_at == user_time
    assert gateway.async_session_store.saves == 0
    saved = store.get_meta("heartbeat_state", {})
    assert saved["last_status"] == "interrupted"
    assert saved["last_reason"] == "real_user_message"


def test_empty_file_and_missing_main_session_skip_without_counting_start(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runtime = SimpleNamespace(store=AgencyStore(config))
    runner = HeartbeatRunner(runtime)
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    (tmp_path / "HEARTBEAT.md").write_text("# HEARTBEAT.md\n<!-- empty -->\n", encoding="utf-8")
    assert asyncio.run(runner.run_once(FakeGateway([]), config, state, reason="scheduled")) is False
    assert "last_started_at" not in state

    (tmp_path / "HEARTBEAT.md").unlink()
    assert asyncio.run(runner.run_once(FakeGateway([]), config, state, reason="scheduled")) is False
    assert "last_started_at" not in state


def test_due_tasks_advance_only_after_completed_model_turn(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    (tmp_path / "HEARTBEAT.md").write_text(
        "tasks:\n  - name: observe\n    interval: 1s\n    prompt: Observe.\n",
        encoding="utf-8",
    )
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="none")
    runtime = SimpleNamespace(store=AgencyStore(config))
    runner = HeartbeatRunner(runtime)
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    entry = FakeEntry(
        "telegram-main",
        datetime(2026, 7, 15, tzinfo=UTC),
        FakeSource(SimpleNamespace(value="telegram"), "chat-1"),
    )

    assert asyncio.run(runner.run_once(FakeGateway([entry]), config, state, reason="task")) is True
    assert state["task_last_runs"]["observe"] > 0
    gateway = FakeGateway([entry])
    state["task_last_runs"]["observe"] = 0
    assert asyncio.run(runner.run_once(gateway, config, state, reason="task")) is True
    assert "Due heartbeat tasks:\n- observe: Observe." in gateway.prompts[0]
