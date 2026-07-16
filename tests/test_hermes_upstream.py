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

from agency.heartbeat import HeartbeatRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_UPSTREAM_COMPAT") != "1",
    reason="requires an upstream Hermes checkout on PYTHONPATH",
)


def test_disposable_heartbeat_session_uses_current_hermes_lifecycle(tmp_path, monkeypatch):
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
    gateway = SimpleNamespace(
        session_store=store,
        _running_agents={},
        _running_agents_ts={},
        _session_model_overrides={},
        _pending_messages={},
    )
    runner = HeartbeatRunner(SimpleNamespace(store=SimpleNamespace()))

    work_source, work = asyncio.run(
        runner._prepare_work_session(gateway, main, source, "compatibility-run")
    )
    assert work_source.platform is Platform.LOCAL
    assert work_source.chat_id == "agency-heartbeat-compatibility-run"
    assert work_source.thread_id == "agency-heartbeat-compatibility-run"
    assert work.session_id != main.session_id
    assert work.session_key != main.session_key
    assert store.lookup_by_session_id(work.session_id) is work

    asyncio.run(runner._cleanup_work_session(gateway, work))

    assert store.lookup_by_session_id(work.session_id) is None
    assert store.lookup_by_session_id(main.session_id) is main
    assert store._db.get_session(work.session_id) is None
    assert store._db.get_session(main.session_id) is not None


def test_stale_heartbeat_sweep_removes_routed_and_orphan_rows(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, GatewayConfig(sessions_dir=sessions_dir))
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="compatibility-chat",
        user_id="compatibility-owner",
    )
    main = store.get_or_create_session(source)
    routed_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="compatibility-chat",
        user_id="compatibility-owner",
        thread_id="agency-heartbeat-" + "a" * 32,
    )
    orphan_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="compatibility-chat",
        user_id="compatibility-owner",
        thread_id="agency-heartbeat-" + "b" * 32,
    )
    routed = store.get_or_create_session(routed_source)
    orphan = store.get_or_create_session(orphan_source)
    with store._lock:
        store._entries.pop(orphan.session_key)
        store._save()
    gateway = SimpleNamespace(
        session_store=store,
        _running_agents={},
        _running_agents_ts={},
        _session_model_overrides={},
        _pending_messages={},
    )
    runner = HeartbeatRunner(SimpleNamespace(store=SimpleNamespace()))

    result = asyncio.run(runner._cleanup_stale_work_sessions(gateway))

    assert result == {"removed": 2, "errors": 0}
    assert store.lookup_by_session_id(main.session_id) is main
    assert store.lookup_by_session_id(routed.session_id) is None
    assert store._db.get_session(routed.session_id) is None
    assert store._db.get_session(orphan.session_id) is None
