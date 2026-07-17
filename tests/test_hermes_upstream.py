"""Opt-in compatibility checks against a real Hermes checkout.

Set ``HERMES_UPSTREAM_COMPAT=1`` and put the checkout first on
``PYTHONPATH``.  The ordinary unit suite remains self-contained; the release
gate runs this test against current upstream Hermes.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from agency.heartbeat import (
    HEARTBEAT_TRANSCRIPT_NOTE,
    HeartbeatRunner,
    HeartbeatTurn,
    _heartbeat_turn,
    _patch_agent_persistence,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_UPSTREAM_COMPAT") != "1",
    reason="requires an upstream Hermes checkout on PYTHONPATH",
)


class _StateStore:
    def __init__(self):
        self.meta = {}

    def set_meta(self, key, value):
        self.meta[key] = value

    def update_decision_delivery(self, decision_id, status):
        return True


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return SimpleNamespace(success=True)


def test_assistant_heartbeat_commits_to_current_hermes_session(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    gateway_config = GatewayConfig(sessions_dir=sessions_dir)
    store = SessionStore(sessions_dir, gateway_config)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="compatibility-chat",
        user_id="compatibility-owner",
    )
    main = store.get_or_create_session(source)
    store.append_to_transcript(
        main.session_id,
        {"role": "user", "content": "baseline", "timestamp": 1.0},
    )
    adapter = _Adapter()
    gateway = SimpleNamespace(
        session_store=store,
        _running_agents={},
        _adapter_for_source=lambda candidate: adapter if candidate == source else None,
        _refresh_agent_cache_message_count=lambda *args: None,
    )
    runner = HeartbeatRunner(SimpleNamespace(store=_StateStore()))
    baseline = asyncio.run(runner._raw_transcript(gateway, main.session_id))
    turn = HeartbeatTurn(
        run_id="compatibility-run",
        prompt="heartbeat",
        target_session_id=main.session_id,
        session_key=main.session_key,
        baseline_transcript=baseline,
        baseline_captured=True,
        state={},
    )
    asyncio.run(runner._commit_conversation_output(gateway, source, turn, "visible heartbeat"))

    committed = asyncio.run(runner._raw_transcript(gateway, main.session_id))
    assert committed[:-2] == baseline
    assert committed[-2]["role"] == "system"
    assert committed[-2]["content"].startswith(HEARTBEAT_TRANSCRIPT_NOTE)
    assert committed[-1]["role"] == "assistant"
    assert committed[-1]["content"] == "visible heartbeat"
    assert turn.transcript_committed is True
    assert turn.delivery_status == "delivered"
    assert adapter.sent == [("compatibility-chat", "visible heartbeat", None)]
    assert store.lookup_by_session_id(main.session_id) is main
    assert store._db.get_session(main.session_id) is not None


def test_current_hermes_agent_persistence_can_be_suppressed_for_hidden_trigger():
    from run_agent import AIAgent

    assert callable(getattr(AIAgent, "_persist_session", None))
    assert callable(getattr(AIAgent, "_flush_messages_to_session_db", None))
    assert _patch_agent_persistence() is True

    agent = object.__new__(AIAgent)
    agent.session_id = "compatibility-session"
    turn = HeartbeatTurn(
        run_id="compatibility-run",
        prompt="heartbeat",
        target_session_id=agent.session_id,
    )
    token = _heartbeat_turn.set(turn)
    try:
        assert agent._persist_session([], []) is None
        assert agent._flush_messages_to_session_db([], []) is None
    finally:
        _heartbeat_turn.reset(token)


def test_current_hermes_gateway_exposes_required_real_turn_hooks():
    from gateway.run import GatewayRunner

    assert callable(getattr(GatewayRunner, "_finish_startup_restore", None))
    assert callable(getattr(GatewayRunner, "_handle_message_with_agent", None))
