"""Authoritative human-vs-internal turn classification for Hermes hooks."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Any

_LOCAL_PLATFORMS = {"", "cli", "local", "shell", "terminal"}
_INTERNAL_PLATFORMS = {"background", "cron", "kanban", "subagent", "system"}
_INTERNAL_SESSION_PREFIXES = (
    "background_",
    "compression_",
    "cron_",
    "kanban_",
    "subagent_",
)
_INTERNAL_HARNESS_PREFIXES = (
    "Review the conversation above and consider saving to memory if appropriate.",
    "Review the conversation above and update the skill library.",
    "[IMPORTANT: Background process ",
    "[ASYNC DELEGATION COMPLETE",
    "[ASYNC DELEGATION BATCH COMPLETE",
    "[Session was just handed off from CLI",
    "[CRITICAL — MESSAGE RECALLED]",
)
_INTERNAL_ORIGINS = {
    "background",
    "background_review",
    "compression",
    "cron",
    "delegation",
    "internal",
    "kanban",
    "recalled_message",
    "system",
}
_USER_ORIGINS = {"human", "inbound", "user", "user_message"}


class _GatewayDispatchMarker:
    """A single-use marker shared by copied ContextVar contexts."""

    def __init__(self) -> None:
        self._consumed = False
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True


_gateway_user_dispatch: ContextVar[_GatewayDispatchMarker | None] = ContextVar(
    "conscious_agency_gateway_user_dispatch", default=None
)
_capture_current_turn: ContextVar[bool] = ContextVar(
    "conscious_agency_capture_current_turn", default=False
)
_turn_records: dict[str, tuple[bool, float]] = {}
_turn_records_lock = threading.Lock()
_TURN_RECORD_TTL_SECONDS = 3600
_MAX_TURN_RECORDS = 1024


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _is_harness_message(message: Any) -> bool:
    text = str(message or "").lstrip()
    return any(text.startswith(prefix) for prefix in _INTERNAL_HARNESS_PREFIXES)


def mark_gateway_user_dispatch(event: Any = None, **_: Any) -> None:
    marker = None
    if event is not None and not getattr(event, "internal", False):
        marker = _GatewayDispatchMarker()
    _gateway_user_dispatch.set(marker)


def _explicit_origin(kwargs: dict[str, Any]) -> str:
    if kwargs.get("internal") is True or kwargs.get("is_internal") is True:
        return "internal"
    if kwargs.get("delivery_visible") is False or kwargs.get("user_visible") is False:
        return "internal"
    for key in (
        "turn_origin",
        "execution_context",
        "agent_context",
        "write_context",
        "write_origin",
        "message_origin",
    ):
        value = _clean(kwargs.get(key)).casefold()
        if value in _USER_ORIGINS:
            return "user"
        if value in _INTERNAL_ORIGINS:
            return "internal"
    return ""


def begin_llm_turn(
    *,
    session_id: Any,
    platform: Any,
    user_message: Any,
    turn_id: Any = "",
    kwargs: dict[str, Any] | None = None,
) -> bool:
    """Classify and bind the current non-cron LLM turn."""

    metadata = dict(kwargs or {})
    explicit = _explicit_origin(metadata)
    session = _clean(session_id).casefold()
    surface = _clean(platform).casefold()
    if explicit:
        capture = explicit == "user"
    elif session.startswith(_INTERNAL_SESSION_PREFIXES) or surface in _INTERNAL_PLATFORMS:
        capture = False
    elif surface not in _LOCAL_PLATFORMS:
        marker = _gateway_user_dispatch.get()
        capture = bool(marker and marker.consume())
    else:
        capture = not _is_harness_message(user_message)
    _capture_current_turn.set(capture)
    clean_turn_id = _clean(turn_id)
    if clean_turn_id:
        now = time.monotonic()
        with _turn_records_lock:
            _prune_turn_records_locked(now)
            _turn_records[clean_turn_id] = (capture, now)
    # Prevent nested review agents from inheriting the user's gateway marker.
    _gateway_user_dispatch.set(None)
    return capture


def _prune_turn_records_locked(now: float) -> None:
    for key, (_, created_at) in list(_turn_records.items()):
        if now - created_at > _TURN_RECORD_TTL_SECONDS:
            _turn_records.pop(key, None)
    if len(_turn_records) <= _MAX_TURN_RECORDS:
        return
    overflow = len(_turn_records) - _MAX_TURN_RECORDS
    for key, _ in sorted(_turn_records.items(), key=lambda item: item[1][1])[:overflow]:
        _turn_records.pop(key, None)


def should_capture_current_turn(turn_id: Any = "") -> bool:
    clean_turn_id = _clean(turn_id)
    if clean_turn_id:
        now = time.monotonic()
        with _turn_records_lock:
            _prune_turn_records_locked(now)
            record = _turn_records.get(clean_turn_id)
        if record is not None:
            return bool(record[0])
    return bool(_capture_current_turn.get())


def finish_llm_turn(turn_id: Any = "") -> None:
    clean_turn_id = _clean(turn_id)
    if clean_turn_id:
        with _turn_records_lock:
            _turn_records.pop(clean_turn_id, None)
    _capture_current_turn.set(False)
    _gateway_user_dispatch.set(None)


def reset_origin_state() -> None:
    finish_llm_turn()
    with _turn_records_lock:
        _turn_records.clear()
