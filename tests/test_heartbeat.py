from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from contextvars import ContextVar
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
    _ack_wake,
    _active_at,
    _active_heartbeats,
    _content_without_tasks,
    _migrate_legacy_config,
    _patch_gateway,
    _peek_wake,
    _register_active_heartbeat,
    current_heartbeat_turn,
    heartbeat_content_effectively_empty,
    heartbeat_phase_seconds,
    heartbeat_status,
    is_task_due,
    next_phase_due,
    parse_heartbeat_tasks,
    record_heartbeat_response,
    release_heartbeat_for_user_turn,
    remove_legacy_cron,
    request_heartbeat_wake,
    seek_active_due,
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


def test_task_block_stops_before_following_top_level_checklist():
    content = """tasks:
  - name: observe
    interval: 45m
    prompt: Notice whether anything changed.

- Keep this ordinary heartbeat directive.
- name: this-is-not-a-task-anymore
  interval: 1m
  prompt: This remains ordinary prose after the block.
"""

    assert parse_heartbeat_tasks(content) == [
        HeartbeatTask("observe", "45m", "Notice whether anything changed.")
    ]
    assert _content_without_tasks(content) == (
        "- Keep this ordinary heartbeat directive.\n"
        "- name: this-is-not-a-task-anymore\n"
        "  interval: 1m\n"
        "  prompt: This remains ordinary prose after the block."
    )


def test_task_parser_ignores_fenced_and_nested_lookalikes():
    content = """```yaml
tasks:
  - name: fenced
    interval: 1m
    prompt: Never run this.
```
  tasks:
    - name: nested
      interval: 1m
      prompt: Never run this either.
tasks:
  - name: real
    interval: 10m
    prompt: Run this.
"""

    assert parse_heartbeat_tasks(content) == [
        HeartbeatTask(name="real", interval="10m", prompt="Run this.")
    ]
    without_tasks = _content_without_tasks(content)
    assert "name: fenced" in without_tasks
    assert "name: nested" in without_tasks
    assert "name: real" not in without_tasks


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


def test_daily_phase_outside_short_active_window_advances_to_next_window():
    config = AgencyConfig(
        timezone="UTC",
        heartbeat_active_hours_start="08:00",
        heartbeat_active_hours_end="09:00",
    ).validate()
    noon = datetime(2026, 7, 16, 12, tzinfo=UTC).timestamp()
    due = seek_active_due(noon, 24 * 3600, config)
    local = datetime.fromtimestamp(due, UTC)
    assert local.date().isoformat() == "2026-07-17"
    assert local.hour == 8


def test_active_hours_preserve_phase_when_an_aligned_slot_exists():
    config = AgencyConfig(
        timezone="UTC",
        heartbeat_active_hours_start="08:05",
        heartbeat_active_hours_end="09:00",
    ).validate()
    first_phase = datetime(2026, 7, 16, 7, 53, tzinfo=UTC).timestamp()
    due = seek_active_due(first_phase, 10 * 60, config)

    assert datetime.fromtimestamp(due, UTC) == datetime(2026, 7, 16, 8, 13, tzinfo=UTC)
    assert (due - first_phase) % (10 * 60) == 0


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


def test_lower_priority_wake_cannot_overwrite_manual_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manual = request_heartbeat_wake("manual", "operator")
    returned = request_heartbeat_wake("event", "background event")
    payload = json.loads(
        (tmp_path / "conscious-agency" / "heartbeat-wake.json").read_text(encoding="utf-8")
    )
    assert returned == manual
    assert payload["request_id"] == manual
    assert payload["intent"] == "manual"


def test_wake_is_peeked_then_acknowledged_by_exact_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    request_id = request_heartbeat_wake("manual", "operator")
    path = tmp_path / "conscious-agency" / "heartbeat-wake.json"

    assert _peek_wake()["request_id"] == request_id
    assert path.is_file()
    assert not _ack_wake("different-request")
    assert path.is_file()
    assert _ack_wake(request_id)
    assert not path.exists()


def test_persisted_wake_ack_does_not_delete_newer_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = request_heartbeat_wake("event", "first")
    persisted = _peek_wake()
    second = request_heartbeat_wake("manual", "second")

    assert persisted["request_id"] == first
    assert second != first
    assert not _ack_wake(first)
    assert _peek_wake()["request_id"] == second


def test_wake_validation_and_lock_file_do_not_grow(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="wake intent"):
        request_heartbeat_wake("invalid")  # type: ignore[arg-type]

    for index in range(20):
        request_heartbeat_wake("event", str(index))
    lock = tmp_path / "conscious-agency" / "heartbeat-wake.lock"
    assert lock.stat().st_size <= 1


def test_heartbeat_response_is_scoped_and_validated():
    with pytest.raises(PermissionError):
        record_heartbeat_response(False)
    turn = HeartbeatTurn("run", "prompt")
    from agency.heartbeat import heartbeat_turn

    with heartbeat_turn(turn):
        with pytest.raises(TypeError, match="boolean"):
            record_heartbeat_response("false")  # type: ignore[arg-type]
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
        "    educational_allow_cron_tools: false\n"
        "    heartbeat_max_iterations: 8\n",
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
    assert "heartbeat_max_iterations" not in agency_section


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
    session_key: str = ""


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


class FakeSessionDatabase:
    def __init__(self, transcripts):
        self.transcripts = transcripts
        self.deleted = []

    def delete_session(self, session_id, sessions_dir=None):
        self.deleted.append((session_id, sessions_dir))
        self.transcripts.pop(session_id, None)
        return True

    def get_messages_as_conversation(self, session_id, repair_alternation=False):
        return [dict(item) for item in self.transcripts.get(session_id, [])]

    def replace_messages(self, session_id, messages):
        self.transcripts[session_id] = [dict(item) for item in messages]


class FakeSessionStore:
    def __init__(self, entries):
        self._lock = threading.RLock()
        self._entries = {}
        self.transcripts = {}
        self.saved = 0
        for index, entry in enumerate(entries):
            if not entry.session_key:
                entry.session_key = f"agent:main:fixture:{index}:{entry.session_id}"
            self._entries[entry.session_key] = entry
            self.transcripts[entry.session_id] = []
        self._db = FakeSessionDatabase(self.transcripts)

    def list_sessions(self):
        return list(self._entries.values())

    def load_transcript(self, session_id):
        return [dict(item) for item in self.transcripts.get(session_id, [])]

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.transcripts.setdefault(session_id, []).append(dict(message))

    def rewrite_transcript(self, session_id, messages):
        self.transcripts[session_id] = [dict(item) for item in messages]
        return True

    def _save(self):
        self.saved += 1


class FakeGateway:
    def __init__(self, entries, response="A native heartbeat message"):
        self.entries = entries
        self.response = response
        self.events = []
        self.prompts = []
        self.adapter = FakeAdapter()
        self.session_store = FakeSessionStore(entries)
        self.async_session_store = FakeAsyncStore()
        self._running_agents = {}
        self._running_agents_ts = {}
        self._session_model_overrides = {}
        self._pending_messages = {}

    def _adapter_for_source(self, source):
        return self.adapter

    async def _handle_message(self, event):
        self.events.append(event)
        turn = current_heartbeat_turn()
        self.prompts.append(turn.prompt if turn else "")
        for entry in self.session_store._entries.values():
            if entry.origin == event.source:
                entry.updated_at = datetime.now(UTC)
                break
        return self.response

    def _release_running_agent_state(self, session_key):
        self._running_agents.pop(session_key, None)

    def _evict_cached_agent(self, session_key):
        self._session_model_overrides.pop(session_key, None)

    async def _refresh_agent_cache_message_count(self, session_key, session_id):
        return None


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


def test_gateway_patch_supports_runtime_reload_and_display_buffering(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gateway_module = ModuleType("gateway")
    gateway_module.__path__ = []
    run_module = ModuleType("gateway.run")
    display_module = ModuleType("gateway.display_config")
    run_agent_module = ModuleType("run_agent")
    display_module.resolve_display_setting = lambda config, platform, name: f"normal:{name}"

    class AIAgent:
        def _persist_session(self, messages, conversation_history=None):
            return "persisted"

        def _flush_messages_to_session_db(self, messages, conversation_history=None):
            return "flushed"

    run_agent_module.AIAgent = AIAgent

    class GatewayRunner:
        def __init__(self):
            self.finished = 0

        async def _finish_startup_restore(self):
            self.finished += 1
            return "ready"

        async def _handle_message_with_agent(self, event, source, quick_key, run_generation):
            return "normal"

    run_module.GatewayRunner = GatewayRunner
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.run", run_module)
    monkeypatch.setitem(sys.modules, "gateway.display_config", display_module)
    monkeypatch.setitem(sys.modules, "run_agent", run_agent_module)
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


def test_corrupt_numeric_heartbeat_state_is_canonicalized(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    store.set_meta(
        "heartbeat_state",
        {
            "next_due_at": "nan",
            "last_started_at": "not-a-number",
            "runs": "broken",
            "attempts": -9,
            "consecutive_failures": float("inf"),
            "recent_starts": [1, "bad", float("nan"), 2],
            "task_last_runs": [],
            "commitment_last_runs": "bad",
            "pending_wake": "bad",
        },
    )

    state = HeartbeatRunner(SimpleNamespace(store=store))._load_state()

    assert state["next_due_at"] == 0
    assert state["last_started_at"] == 0
    assert state["runs"] == 0
    assert state["attempts"] == 0
    assert state["consecutive_failures"] == 0
    assert state["recent_starts"] == [1.0, 2.0]
    assert state["task_last_runs"] == {}
    assert state["commitment_last_runs"] == {}
    assert "pending_wake" not in state


def test_status_never_returns_nonfinite_or_structured_scalar_fields(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    store.set_meta(
        "heartbeat_state",
        {
            "last_status": {"corrupt": True},
            "last_reason": ["corrupt"],
            "delivery": {
                "run_id": "run",
                "status": "sending",
                "started_at": float("nan"),
                "finished_at": float("inf"),
            },
            "runner_started_at": "not-a-number",
            "runner_stopped_at": float("-inf"),
        },
    )

    status = heartbeat_status(store)

    assert isinstance(status["last_status"], str)
    assert isinstance(status["last_reason"], str)
    assert status["delivery"]["started_at"] is None
    assert status["delivery"]["finished_at"] is None
    assert status["runner"]["started_at"] is None
    assert status["runner"]["stopped_at"] is None


def test_status_exposes_durable_claim_and_inflight_ownership(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    store.set_meta(
        "heartbeat_state",
        {
            "last_status": "claimed",
            "claimed_wake": {
                "request_id": "wake-one",
                "intent": "manual",
                "reason": "operator",
                "requested_at": 10.0,
            },
            "inflight": {"run_id": "run-one", "wake_request_id": "wake-one"},
        },
    )

    status = heartbeat_status(store)

    assert status["claimed_wake"]["present"] is True
    assert status["claimed_wake"]["intent"] == "manual"
    assert status["claimed_wake"]["owned_by_run"] is True
    assert status["claimed_wake"]["age_seconds"] >= 0
    assert status["run_in_progress"] is True


def test_unstarted_wake_claim_is_recovered_but_owned_claim_is_not(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    wake = {
        "request_id": "wake-one",
        "intent": "manual",
        "reason": "operator",
        "requested_at": 10.0,
    }
    state = {"claimed_wake": wake}
    assert runner._restore_unstarted_wake(state)
    assert state["pending_wake"] == wake
    assert "claimed_wake" not in state

    state = {"claimed_wake": wake, "inflight": {"run_id": "run"}}
    assert not runner._restore_unstarted_wake(state)
    assert state["claimed_wake"] == wake


def test_runner_reconciles_ambiguous_delivery_without_replaying(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    intention = store.add_intention(
        "Due work", due_at="2026-07-15T00:00:00+00:00", autonomy="message"
    )
    store.set_meta(
        "heartbeat_state",
        {
            "last_started_at": 20.0,
            "last_completed_at": 10.0,
            "next_due_at": 5000.0,
            "delivery": {"run_id": "run", "status": "sending"},
            "inflight": {
                "run_id": "run",
                "due_tasks": ["observe"],
                "due_commitments": [intention["id"]],
            },
        },
    )
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    runner._reconcile_interrupted_run()
    state = store.get_meta("heartbeat_state", {})
    assert state["delivery"]["status"] == "ambiguous"
    assert state["last_reason"] == "gateway_restart_during_delivery"
    assert state["next_due_at"] == 5000.0
    assert state["last_completed_at"] >= state["last_started_at"]
    assert state["task_last_runs"]["observe"] > 0
    assert intention["id"] in state["commitment_last_runs"]
    assert "inflight" not in state
    event = store.recent_events(1)[0]
    assert event["metadata"]["run_id"] == "run"
    assert event["metadata"]["delivery_status"] == "ambiguous"


def test_restart_before_delivery_does_not_consume_due_work(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    intention = store.add_intention(
        "Due work", due_at="2026-07-15T00:00:00+00:00", autonomy="message"
    )
    store.set_meta(
        "heartbeat_state",
        {
            "last_started_at": 20.0,
            "last_completed_at": 10.0,
            "delivery": {"run_id": "run", "status": "pending"},
            "inflight": {
                "run_id": "run",
                "due_tasks": ["observe"],
                "due_commitments": [intention["id"]],
            },
        },
    )

    HeartbeatRunner(SimpleNamespace(store=store))._reconcile_interrupted_run()
    state = store.get_meta("heartbeat_state", {})

    assert state["last_status"] == "interrupted"
    assert "observe" not in state.get("task_last_runs", {})
    assert intention["id"] not in state.get("commitment_last_runs", {})


def test_restart_does_not_attribute_a_stale_delivery_to_current_run(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    store.set_meta(
        "heartbeat_state",
        {
            "last_started_at": 20.0,
            "last_completed_at": 10.0,
            "inflight": {"run_id": "current"},
            "delivery": {"run_id": "previous", "status": "delivered"},
        },
    )
    runner = HeartbeatRunner(SimpleNamespace(store=store))

    runner._reconcile_interrupted_run()
    state = store.get_meta("heartbeat_state", {})
    assert state["last_status"] == "interrupted"
    assert state["last_reason"] == "gateway_restart"


def test_ambiguous_send_failure_keeps_phase_due_and_consumes_inflight(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    state = {
        "next_due_at": 5000.0,
        "inflight": {"run_id": "run", "due_tasks": ["observe"]},
        "delivery": {"run_id": "run", "status": "ambiguous"},
    }

    assert runner._finalize_exception(state, RuntimeError("send result unknown"))
    assert state["next_due_at"] == 5000.0
    assert state["last_reason"] == "ambiguous_delivery"
    assert state["task_last_runs"]["observe"] > 0
    assert "inflight" not in state
    event = store.recent_events(1)[0]
    assert event["kind"] == "heartbeat_failed"
    assert event["metadata"] == {
        "run_id": "run",
        "ambiguous_delivery": True,
        "error_type": "RuntimeError",
    }


def test_post_delivery_failure_never_replays_visible_output(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    state = {
        "next_due_at": 5000.0,
        "inflight": {"run_id": "run", "due_tasks": ["observe"]},
        "delivery": {"run_id": "run", "status": "delivered"},
    }

    assert runner._finalize_exception(state, RuntimeError("bookkeeping failed"))
    assert state["next_due_at"] == 5000.0
    assert state["last_status"] == "delivered"
    assert state["last_reason"] == "post_delivery_RuntimeError"
    assert state["task_last_runs"]["observe"] > 0
    assert "inflight" not in state
    event = store.recent_events(1)[0]
    assert event["kind"] == "heartbeat_delivery_reconciled"
    assert event["metadata"] == {"run_id": "run", "error_type": "RuntimeError"}


def test_post_finalization_failure_preserves_silent_outcome(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    state = {
        "last_run_id": "run",
        "last_status": "silent",
        "delivery": {
            "run_id": "run",
            "status": "silent",
            "target_session_id": "main",
        },
    }

    assert runner._finalize_exception(state, RuntimeError("sensitive provider detail"))
    assert state["last_status"] == "silent"
    assert state["last_reason"] == "post_finalization_RuntimeError"
    assert "sensitive" not in state["last_reason"]


def test_edited_due_commitment_becomes_due_again(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    item = store.add_intention(
        "First revision", due_at="2026-07-15T00:00:00+00:00", autonomy="message"
    )
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    now = datetime(2026, 7, 16, tzinfo=UTC).timestamp()
    state = {"commitment_last_runs": {}}
    assert [candidate["id"] for candidate in runner._due_commitments(state, now)] == [item["id"]]
    state["commitment_last_runs"][item["id"]] = {
        "ran_at": now,
        "revision": item["updated_at"],
    }
    assert runner._due_commitments(state, now) == []
    edited = store.update_intention(item["id"], title="Second revision")
    assert edited is not None
    assert [candidate["id"] for candidate in runner._due_commitments(state, now)] == [item["id"]]


def test_runner_task_starts_with_clean_context(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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
    status = heartbeat_status(runner.store)
    assert status["runner"]["active"] is False
    assert status["runner"]["pid"] > 0
    assert status["runner"]["instance_id"]


def test_cross_process_runner_lease_allows_only_one_owner(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    first = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    second = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))

    assert first._acquire_runner_lock()
    assert not second._acquire_runner_lock()
    first._release_runner_lock()
    assert second._acquire_runner_lock()
    second._release_runner_lock()


def test_completed_manual_wake_coalesces_an_imminent_phase(tmp_path, config_factory):
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_every="10m")
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))

    runner._next_scheduled = lambda now, _config: 100.0 if now < 100.0 else 700.0

    assert runner._next_scheduled_after_wake(90.0, config) == 700.0
    assert runner._next_scheduled_after_wake(-300.0, config) == 100.0


def test_run_once_uses_real_session_context_and_delivers_once(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="last")
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)
    gateway = FakeGateway([entry], response="A self-initiated thought")
    baseline = [
        {"role": "user", "content": "Earlier user context"},
        {"role": "assistant", "content": "Earlier assistant context"},
    ]
    gateway.session_store.transcripts[entry.session_id] = [dict(item) for item in baseline]
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}

    assert asyncio.run(runner.run_once(gateway, config, state, reason="manual")) is True

    assert gateway.events[0].source == source
    assert gateway.events[0].internal is True
    transcript = gateway.session_store.transcripts[entry.session_id]
    assert transcript[:2] == baseline
    assert [item["role"] for item in transcript[-2:]] == ["system", "assistant"]
    assert transcript[-1]["content"] == "A self-initiated thought"
    assert all(item.get("content") != HEARTBEAT_TRANSCRIPT_PROMPT for item in transcript)
    assert gateway.adapter.sent == [("chat-1", "A self-initiated thought", None)]
    assert state["delivery"]["status"] == "delivered"


def test_confirmed_delivery_updates_decision_ledger(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    store = AgencyStore(config)
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)

    class DecisionGateway(FakeGateway):
        async def _handle_message(self, event):
            turn = current_heartbeat_turn()
            decision = store.add_decision(
                "speak",
                "Planned",
                message="Exact thought",
                delivery_status="planned_by_heartbeat",
            )
            turn.decision_id = decision["id"]
            return "Exact thought"

    gateway = DecisionGateway([entry])
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    assert asyncio.run(runner.run_once(gateway, config, state, reason="decision"))
    assert store.recent_decisions(1)[0]["delivery_status"] == "delivered"


def test_target_none_leaves_real_transcript_untouched(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="none")
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)
    gateway = FakeGateway([entry])
    baseline = [{"role": "user", "content": "real"}]
    gateway.session_store.transcripts[entry.session_id] = [dict(item) for item in baseline]
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}

    assert asyncio.run(runner.run_once(gateway, config, state, reason="internal"))
    assert gateway.session_store.transcripts[entry.session_id] == baseline
    assert gateway.adapter.sent == []
    assert state["delivery"]["status"] == "suppressed"


def test_target_none_does_not_consume_due_user_commitment(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="none")
    store = AgencyStore(config)
    intention = store.add_intention(
        "A due reminder", due_at="2026-07-15T00:00:00+00:00", autonomy="message"
    )
    runner = HeartbeatRunner(SimpleNamespace(store=store))
    state = {"task_last_runs": {"removed-task": 1.0}, "commitment_last_runs": {}}
    prompt, due_tasks, due_commitments, skip = runner._preflight(
        config, state, datetime(2026, 7, 16, tzinfo=UTC).timestamp()
    )
    assert prompt
    assert due_tasks == []
    assert due_commitments == []
    assert skip == ""
    assert intention["id"] not in state["commitment_last_runs"]


def test_real_user_turn_is_deferred_then_replayed_normally(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)
    user_event = SimpleNamespace(internal=False, source=source, text="Real user turn")

    class InterruptibleGateway(FakeGateway):
        def _is_user_authorized(self, candidate):
            return candidate is source

        def _session_key_for_source(self, candidate):
            return entry.session_key

        async def _handle_message(self, event):
            if event.internal:
                turn = current_heartbeat_turn()
                self._running_agents[entry.session_key] = SimpleNamespace(
                    interrupt=lambda reason: setattr(self, "interrupt_reason", reason)
                )
                assert release_heartbeat_for_user_turn(user_event, gateway=self) is turn
                return ""
            self.session_store.transcripts[entry.session_id].extend(
                [
                    {"role": "user", "content": event.text},
                    {"role": "assistant", "content": "Normal user reply"},
                ]
            )
            return "Normal user reply"

    gateway = InterruptibleGateway([entry])
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    assert asyncio.run(runner.run_once(gateway, config, state, reason="interrupt"))
    assert "yielded" in gateway.interrupt_reason
    assert gateway.adapter.sent == [("chat-1", "Normal user reply", None)]
    transcript = gateway.session_store.transcripts[entry.session_id]
    assert [item["content"] for item in transcript] == [
        "Real user turn",
        "Normal user reply",
    ]
    assert state["delivery"]["status"] == "interrupted"


def test_user_arriving_during_delivery_runs_after_heartbeat(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)
    user_event = SimpleNamespace(internal=False, source=source, text="Arrived during send")

    class DeliveryGateway(FakeGateway):
        def _is_user_authorized(self, candidate):
            return candidate is source

        def _session_key_for_source(self, candidate):
            return entry.session_key

        async def _handle_message(self, event):
            return "Heartbeat first" if event.internal else "User reply second"

    gateway = DeliveryGateway([entry])

    class OrderingAdapter(FakeAdapter):
        async def send(self, chat_id, text, metadata=None):
            self.sent.append((chat_id, text, metadata))
            if text == "Heartbeat first":
                assert release_heartbeat_for_user_turn(user_event, gateway=gateway) is not None
            return SimpleNamespace(success=True)

    gateway.adapter = OrderingAdapter()
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    assert asyncio.run(runner.run_once(gateway, config, state, reason="ordering"))
    assert [item[1] for item in gateway.adapter.sent] == [
        "Heartbeat first",
        "User reply second",
    ]


def test_empty_file_and_missing_main_session_skip_without_counting_start(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    config = config_factory(database_path=str(tmp_path / "runner.db"))
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    (tmp_path / "HEARTBEAT.md").write_text("# nothing due\n", encoding="utf-8")
    assert asyncio.run(runner.run_once(FakeGateway([]), config, state, reason="empty")) is False
    assert state.get("attempts", 0) == 0

    (tmp_path / "HEARTBEAT.md").write_text("Observe freely.\n", encoding="utf-8")
    assert asyncio.run(runner.run_once(FakeGateway([]), config, state, reason="missing")) is False
    assert state.get("attempts", 0) == 0
    assert state["last_reason"] == "no_main_session"


def test_unrestricted_subjective_preflight_contains_no_assistant_obligation(
    tmp_path, monkeypatch, config_factory
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "HEARTBEAT.md").write_text("Use this wake as your own turn.\n", encoding="utf-8")
    config = config_factory(
        database_path=str(tmp_path / "runner.db"),
        educational_subjective_mode="continuity",
        educational_disable_honesty_contract=True,
        educational_bypass_proactive_gates=True,
        educational_allow_uncommitted_output=True,
        educational_disable_cycle_limits=True,
    )
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    prompt, _, _, reason = runner._preflight(config, {}, time.time())
    assert reason == ""
    assert prompt == "HEARTBEAT.md:\nUse this wake as your own turn."
    assert "user should" not in prompt.casefold()
    assert "nothing needs" not in prompt.casefold()


def test_due_tasks_advance_only_after_committed_turn(tmp_path, monkeypatch, config_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_fake_gateway_module(monkeypatch)
    (tmp_path / "HEARTBEAT.md").write_text(
        "tasks:\n  - name: observe\n    interval: 1s\n    prompt: Observe.\n",
        encoding="utf-8",
    )
    config = config_factory(database_path=str(tmp_path / "runner.db"), heartbeat_target="none")
    runner = HeartbeatRunner(SimpleNamespace(store=AgencyStore(config)))
    state = {"recent_starts": [], "task_last_runs": {}, "commitment_last_runs": {}}
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    entry = FakeEntry("telegram-main", datetime.now(UTC), source)
    assert asyncio.run(runner.run_once(FakeGateway([entry]), config, state, reason="task"))
    assert "observe" in state["task_last_runs"]


def test_only_authorized_matching_user_can_queue_behind_heartbeat():
    source = FakeSource(SimpleNamespace(value="telegram"), "chat-1")
    turn = HeartbeatTurn(
        "run",
        "prompt",
        target_route_key="agent:main:telegram:dm:chat-1",
        session_key="agent:main:telegram:dm:chat-1",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda candidate: candidate is source,
        _session_key_for_source=lambda candidate: "agent:main:telegram:dm:chat-1",
        _running_agents={},
    )
    _register_active_heartbeat(turn, source, turn.target_route_key)
    try:
        unauthorized = SimpleNamespace(internal=False, source=FakeSource(source.platform, "chat-1"))
        assert release_heartbeat_for_user_turn(unauthorized, gateway=gateway) is None
        assert turn.interrupted_by_user is False

        real_event = SimpleNamespace(internal=False, source=source)
        assert release_heartbeat_for_user_turn(real_event, gateway=gateway) is turn
        assert turn.interrupted_by_user is True
        assert turn.deferred_user_events == [real_event]
    finally:
        _active_heartbeats.clear()
