"""Hermes hook runtime and proactive-cycle tool isolation."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .config import load_config
from .engine import AgencyEngine
from .store import AgencyStore
from .tools import handle_agency

logger = logging.getLogger(__name__)


def _is_cron_session(session_id: str) -> bool:
    return str(session_id or "").startswith("cron_")


def _is_user_session(session_id: str, platform: str) -> bool:
    session = str(session_id or "").lower()
    surface = str(platform or "").lower()
    internal_prefixes = ("cron_", "subagent_", "kanban_", "compression_", "background_")
    internal_platforms = {"cron", "subagent", "kanban", "system", "background"}
    return not session.startswith(internal_prefixes) and surface not in internal_platforms


class AgencyRuntime:
    def __init__(self):
        self.config = load_config()
        self.store = AgencyStore(self.config)
        self.engine = AgencyEngine(self.store, self.config)
        self._cron_job_id = str(self.store.get_meta("cron_job_id", "") or "")
        self._cycle_lock = threading.RLock()
        self._active_cycles: dict[str, dict[str, Any]] = {}

    def _is_agency_cron_session(self, session_id: str) -> bool:
        try:
            job_id = str(self.store.get_meta("cron_job_id", "") or "")
            if job_id:
                self._cron_job_id = job_id
        except Exception:
            job_id = self._cron_job_id
        return bool(job_id and str(session_id).startswith(f"cron_{job_id}_"))

    def _clean_cycles(self) -> None:
        now = time.monotonic()
        with self._cycle_lock:
            expired = [
                key for key, state in self._active_cycles.items() if float(state["deadline"]) <= now
            ]
            for key in expired:
                self._active_cycles.pop(key, None)

    def bind_cycle(self, task_id: str, session_id: str) -> None:
        if not task_id or not self._is_agency_cron_session(session_id):
            return
        self._clean_cycles()
        with self._cycle_lock:
            self._active_cycles[task_id] = {
                "deadline": time.monotonic() + 900,
                "reflections": 0,
                "state_changes": 0,
                "session_id": session_id,
                "committed_output": None,
            }

    def end_cycle(self, task_id: str) -> None:
        if task_id:
            with self._cycle_lock:
                self._active_cycles.pop(task_id, None)

    def is_active_cycle(self, task_id: str) -> bool:
        self._clean_cycles()
        with self._cycle_lock:
            return bool(task_id and task_id in self._active_cycles)

    def _task_for_session(self, session_id: str) -> str:
        self._clean_cycles()
        with self._cycle_lock:
            for task_id, state in self._active_cycles.items():
                if state.get("session_id") == session_id:
                    return task_id
        return ""

    def _fail_closed_cycle(self, task_id: str, reason: str) -> None:
        try:
            self.engine.record_decision("silent", reason)
        except Exception as exc:
            logger.warning("conscious-agency fail-closed decision could not be recorded: %s", exc)
        finally:
            self.end_cycle(task_id)

    def tool_handler(self, args: dict[str, Any], **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "")
        session_id = str(kwargs.get("session_id") or "")
        action = str(args.get("action") or "status").strip().lower()
        if action == "tick":
            self.bind_cycle(task_id, session_id)
        change_actions = {
            "add_intention",
            "update_intention",
            "set_focus",
            "clear_focus",
            "add_question",
            "resolve_question",
            "add_self_observation",
        }
        if self.is_active_cycle(task_id):
            with self._cycle_lock:
                cycle = self._active_cycles.get(task_id, {})
                if cycle.get("committed_output") is not None:
                    return json.dumps(
                        {
                            "success": False,
                            "error": "cycle already committed; return the committed delivery_text",
                        }
                    )
                if action == "add_reflection" and int(cycle.get("reflections", 0)) >= (
                    self.config.maximum_reflections_per_tick
                ):
                    return json.dumps(
                        {"success": False, "error": "reflection limit reached for this tick"}
                    )
                if action in change_actions and int(cycle.get("state_changes", 0)) >= (
                    self.config.maximum_state_changes_per_tick
                ):
                    return json.dumps(
                        {"success": False, "error": "state-change limit reached for this tick"}
                    )
        if action == "record_decision" and not self.is_active_cycle(task_id):
            return json.dumps(
                {
                    "success": False,
                    "error": "record_decision is only valid after tick in the same proactive cycle",
                }
            )
        result = handle_agency(self.engine, args, **kwargs)
        try:
            payload = json.loads(result)
            succeeded = bool(payload.get("success"))
        except (TypeError, ValueError):
            payload = {}
            succeeded = False
        if succeeded and self.is_active_cycle(task_id):
            with self._cycle_lock:
                cycle = self._active_cycles.get(task_id)
                if cycle is not None and action == "add_reflection":
                    cycle["reflections"] = int(cycle.get("reflections", 0)) + 1
                if cycle is not None and action in change_actions:
                    cycle["state_changes"] = int(cycle.get("state_changes", 0)) + 1
        if action == "record_decision" and succeeded:
            delivery_text = (payload.get("result") or {}).get("delivery_text")
            with self._cycle_lock:
                cycle = self._active_cycles.get(task_id)
                if cycle is not None:
                    cycle["committed_output"] = str(delivery_text or "[SILENT]")
        return result

    def transform_llm_output(
        self,
        response_text: str = "",
        session_id: str = "",
        **_: Any,
    ) -> str | None:
        """Make the committed agency decision authoritative for cron delivery."""
        task_id = self._task_for_session(session_id)
        if not task_id and not self._is_agency_cron_session(session_id):
            return None
        if not task_id:
            try:
                self.engine.record_decision(
                    "silent",
                    "Fail-closed: scheduled cycle ended without calling tick",
                )
            except Exception as exc:
                logger.warning("conscious-agency missing-tick decision failed: %s", exc)
            return "[SILENT]"
        with self._cycle_lock:
            committed = self._active_cycles.get(task_id, {}).get("committed_output")
        if not committed:
            self._fail_closed_cycle(
                task_id,
                "Fail-closed: model ended scheduled cycle without record_decision",
            )
            return "[SILENT]"
        self.end_cycle(task_id)
        return str(committed)

    def pre_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        task_id: str = "",
        **_: Any,
    ) -> dict[str, str] | None:
        if self.is_active_cycle(task_id) and tool_name != "conscious_agency":
            return {
                "action": "block",
                "message": (
                    "Conscious Agency safety boundary: proactive cycles are conversation-only. "
                    "Only the conscious_agency tool may be used after tick; record a silent or "
                    "speak decision instead."
                ),
            }
        return None

    def post_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        task_id: str = "",
        session_id: str = "",
        duration_ms: int = 0,
        **_: Any,
    ) -> None:
        try:
            failed = isinstance(result, str) and ('"error"' in result or "Error:" in result)
            self.store.add_event(
                "tool_call",
                session_id=session_id,
                task_id=task_id,
                summary=tool_name[:200],
                metadata={"duration_ms": int(duration_ms or 0), "failed": failed},
            )
        except Exception as exc:
            logger.debug("conscious-agency post_tool_call failed: %s", exc)

    def pre_llm_call(
        self,
        session_id: str = "",
        user_message: str = "",
        platform: str = "",
        task_id: str = "",
        **_: Any,
    ) -> dict[str, str] | None:
        try:
            if _is_cron_session(session_id):
                task = self._task_for_session(session_id)
                if task:
                    self._fail_closed_cycle(
                        task,
                        "Fail-closed: output transform was not applied before post_llm_call",
                    )
                self.store.add_event(
                    "cron_turn_started",
                    session_id=session_id,
                    task_id=task_id,
                    summary="Scheduled agent turn started",
                )
            elif _is_user_session(session_id, platform) and user_message.strip():
                self.engine.record_user_turn(
                    user_message,
                    session_id=session_id,
                    task_id=task_id,
                    platform=platform,
                )
            if self.config.inject_context and self.config.enabled:
                return {"context": self.engine.context_block()}
        except Exception as exc:
            logger.warning("conscious-agency pre_llm_call failed: %s", exc)
        return None

    def post_llm_call(
        self,
        session_id: str = "",
        assistant_response: str = "",
        platform: str = "",
        task_id: str = "",
        **_: Any,
    ) -> None:
        try:
            if _is_cron_session(session_id):
                self.store.add_event(
                    "cron_turn_finished",
                    session_id=session_id,
                    task_id=task_id,
                    summary="Scheduled agent turn finished",
                    metadata={"message_chars": len(assistant_response)},
                )
            elif _is_user_session(session_id, platform):
                self.engine.record_assistant_turn(
                    assistant_response,
                    session_id=session_id,
                    task_id=task_id,
                    platform=platform,
                )
            self.store.prune_events()
        except Exception as exc:
            logger.debug("conscious-agency post_llm_call failed: %s", exc)

    def session_event(
        self, kind: str, session_id: str = "", platform: str = "", **kwargs: Any
    ) -> None:
        try:
            metadata = {
                key: value
                for key, value in kwargs.items()
                if key in {"completed", "interrupted", "model"}
                and isinstance(value, (str, bool, int))
            }
            self.store.add_event(
                kind,
                session_id=session_id or "",
                platform=platform or "",
                summary=kind.replace("_", " "),
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("conscious-agency session hook failed: %s", exc)
