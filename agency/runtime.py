"""Hermes hook runtime and proactive-cycle tool isolation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from typing import Any

from .config import load_config
from .engine import SUBJECTIVE_PROTOCOL_VERSION, AgencyEngine, subjective_visible_text
from .heartbeat import (
    HeartbeatRunner,
    HeartbeatTurn,
    current_heartbeat_turn,
    heartbeat_response,
    record_heartbeat_response,
    release_heartbeat_for_user_turn,
    strip_heartbeat_ack,
    unrestricted_subjective_heartbeat,
)
from .origin import (
    begin_llm_turn,
    finish_llm_turn,
    mark_gateway_user_dispatch,
    should_capture_current_turn,
)
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


def _tool_result_failed(result: Any) -> bool:
    """Classify structured tool failures without treating ``error: null`` as an error."""

    if isinstance(result, dict):
        if result.get("success") is False:
            return True
        error = result.get("error")
        return error is not None and error != "" and error is not False
    if not isinstance(result, str):
        return False
    stripped = result.strip()
    if not stripped:
        return False
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        lowered = stripped.lower()
        return lowered.startswith(("error:", "tool error:", "[error]"))
    return _tool_result_failed(parsed)


class AgencyRuntime:
    def __init__(self):
        self.config = load_config()
        self.store = AgencyStore(self.config)
        self.engine = AgencyEngine(self.store, self.config)
        self._heartbeat_runner: HeartbeatRunner | None = None
        self._cycle_lock = threading.RLock()
        self._active_cycles: dict[str, dict[str, Any]] = {}

    def reload_config(self, config: Any | None = None) -> None:
        updated = config or load_config()
        self.config = updated
        self.engine.config = updated

    def ensure_heartbeat(self, gateway: Any) -> None:
        existing = getattr(gateway, "_conscious_agency_heartbeat_runner", None)
        if (
            getattr(existing, "_agency_heartbeat_runner", False)
            and callable(getattr(existing, "rebind", None))
            and callable(getattr(existing, "start", None))
        ):
            existing.rebind(self)
            self._heartbeat_runner = existing
        else:
            self._heartbeat_runner = HeartbeatRunner(self)
            gateway._conscious_agency_heartbeat_runner = self._heartbeat_runner
        self._heartbeat_runner.start(gateway)

    def pre_gateway_dispatch(
        self, event: Any = None, gateway: Any = None, **kwargs: Any
    ) -> dict[str, str] | None:
        released = release_heartbeat_for_user_turn(event, gateway=gateway)
        if released is not None:
            # Hermes' ordinary busy-session path would recursively consume this
            # event inside the heartbeat task. The runner owns the event now and
            # replays it after the real session has been released.
            return {
                "action": "skip",
                "reason": "queued behind assistant-initiated heartbeat",
            }
        # This hook runs before Hermes' authorization gate. Only an event proven
        # authorized by the same gateway check may become the genuine-user marker.
        if gateway is None:
            mark_gateway_user_dispatch(event=event, **kwargs)
        elif callable(getattr(gateway, "_is_user_authorized", None)):
            with contextlib.suppress(Exception):
                if gateway._is_user_authorized(getattr(event, "source", None)):
                    mark_gateway_user_dispatch(event=event, **kwargs)
        if gateway is not None:
            self.ensure_heartbeat(gateway)
        return None

    def llm_request(
        self, request: dict[str, Any], session_id: str = "", **_: Any
    ) -> dict[str, Any] | None:
        """Apply provider-level options for a native Agency heartbeat.

        Hermes request middleware receives the provider kwargs immediately
        before transport execution.  Copy each nested mapping so the original
        request remains untouched. Expressive runs do not receive tool schemas;
        the pre-tool hook remains a second boundary if a provider still emits a call.
        """
        if current_heartbeat_turn() is None:
            return None
        disable_thinking = self.config.heartbeat_disable_thinking
        isolate_tools = self._expressive_subjective_heartbeat()
        decision_recorded = heartbeat_response() is not None
        if (
            not disable_thinking
            and not isolate_tools
            and not decision_recorded
            and "tools" not in request
        ):
            return None
        updated = dict(request)
        metadata: dict[str, bool] = {}
        if disable_thinking:
            extra_body = dict(updated.get("extra_body") or {})
            chat_template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})
            chat_template_kwargs["enable_thinking"] = False
            extra_body["chat_template_kwargs"] = chat_template_kwargs
            updated["extra_body"] = extra_body
            metadata["agency_heartbeat_disable_thinking"] = True
        if isolate_tools or decision_recorded:
            updated.pop("tools", None)
            updated.pop("tool_choice", None)
            updated.pop("parallel_tool_calls", None)
            metadata[
                "agency_heartbeat_decision_finalized"
                if decision_recorded
                else "agency_heartbeat_tool_isolation"
            ] = True
        elif "tools" in updated:
            # Unlimited tool work remains available, but heartbeat side effects
            # are serialized so heartbeat_respond is an actual final decision.
            updated["parallel_tool_calls"] = False
            metadata["agency_heartbeat_tools_serialized"] = True
        return {
            "request": updated,
            "metadata": metadata,
        }

    def _expressive_subjective_heartbeat(self) -> bool:
        return self._unrestricted_subjective_heartbeat() and not (
            self.config.educational_allow_heartbeat_tools
        )

    def _unrestricted_subjective_heartbeat(self) -> bool:
        """Prompt freedom is independent from the separate tool-access choice."""

        return unrestricted_subjective_heartbeat(self.config)

    def _clean_cycles(self) -> None:
        now = time.monotonic()
        with self._cycle_lock:
            expired = [
                key for key, state in self._active_cycles.items() if float(state["deadline"]) <= now
            ]
            for key in expired:
                self._active_cycles.pop(key, None)

    def bind_cycle(self, task_id: str, session_id: str) -> None:
        if not task_id or current_heartbeat_turn() is None:
            return
        self._clean_cycles()
        with self._cycle_lock:
            self._active_cycles[task_id] = {
                "deadline": time.monotonic() + 900,
                "reflections": 0,
                "state_changes": 0,
                "session_id": session_id,
                "committed_output": None,
                "decision_id": "",
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

    def _record_subjective_output(
        self,
        output_text: str,
        *,
        source: str,
        session_id: str,
        turn_id: str = "",
        task_id: str = "",
        model: str = "",
        platform: str = "",
    ) -> bool:
        mode = self.config.educational_subjective_mode
        if mode == "off":
            return True
        if not output_text:
            return False
        stable_turn = str(turn_id or task_id).strip()
        if not stable_turn:
            stable_turn = hashlib.sha256(output_text.encode("utf-8")).hexdigest()[:20]
        capture_key = f"{source}:{session_id}:{stable_turn}"
        try:
            self.store.add_subjective_entry(
                capture_key=capture_key,
                model_id=str(model or "unknown"),
                source=source,
                condition=mode,
                prompt_version=SUBJECTIVE_PROTOCOL_VERSION,
                session_id=session_id,
                output_text=output_text,
                metadata={
                    "platform": str(platform or "")[:80],
                    "capture_stage": "final_output",
                    "turn_origin": "user" if source == "conversation" else "heartbeat",
                },
            )
            return True
        except Exception as exc:
            logger.warning("conscious-agency subjective journal write failed: %s", exc)
            return False

    def _finalize_heartbeat_decision(self, heartbeat: HeartbeatTurn, status: str) -> None:
        if not heartbeat.decision_id:
            return
        try:
            if self.store.update_decision_delivery(heartbeat.decision_id, status):
                heartbeat.decision_finalized = True
        except Exception as exc:
            logger.warning("conscious-agency decision ledger update failed: %s", exc)

    @staticmethod
    def _delivery_text(output_text: str) -> str:
        """Remove a contradictory sentinel only when the model also produced content."""

        visible = subjective_visible_text(output_text)
        return visible if visible else "[SILENT]"

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
                if (
                    not self.config.educational_disable_cycle_limits
                    and action == "add_reflection"
                    and int(cycle.get("reflections", 0)) >= self.config.maximum_reflections_per_tick
                ):
                    return json.dumps(
                        {"success": False, "error": "reflection limit reached for this tick"}
                    )
                if (
                    not self.config.educational_disable_cycle_limits
                    and action in change_actions
                    and int(cycle.get("state_changes", 0))
                    >= self.config.maximum_state_changes_per_tick
                ):
                    return json.dumps(
                        {"success": False, "error": "state-change limit reached for this tick"}
                    )
        if (
            action == "record_decision"
            and not self.is_active_cycle(task_id)
            and not self.config.educational_allow_uncommitted_output
        ):
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
            decision_id = str((payload.get("result") or {}).get("id") or "")
            delivery_text = (payload.get("result") or {}).get("delivery_text")
            with self._cycle_lock:
                cycle = self._active_cycles.get(task_id)
                if cycle is not None:
                    cycle["committed_output"] = str(delivery_text or "[SILENT]")
                    cycle["decision_id"] = decision_id
            heartbeat = current_heartbeat_turn()
            if heartbeat is not None:
                heartbeat.decision_id = decision_id
        return result

    def heartbeat_handler(self, args: dict[str, Any], **_: Any) -> str:
        try:
            result = record_heartbeat_response(
                args.get("notify"),
                str(args.get("notification_text") or ""),
            )
            return json.dumps({"success": True, "result": result}, ensure_ascii=False)
        except (PermissionError, TypeError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    def transform_llm_output(
        self,
        response_text: str = "",
        session_id: str = "",
        model: str = "",
        platform: str = "",
        **_: Any,
    ) -> str | None:
        """Make heartbeat acknowledgement, journal, and decision state authoritative."""
        heartbeat = current_heartbeat_turn()
        if heartbeat is None:
            return None
        journal_session_id = heartbeat.target_session_id or session_id
        heartbeat.transformed = True
        task_id = self._task_for_session(session_id)
        heartbeat.raw_output = response_text
        structured = heartbeat_response()
        if self.config.educational_allow_uncommitted_output:
            self.end_cycle(task_id)
            capture_text = response_text
            if not capture_text and structured is not None:
                capture_text = (
                    str(structured.get("notification_text") or "").strip()
                    if structured.get("notify")
                    else "HEARTBEAT_OK"
                )
            recorded = self._record_subjective_output(
                capture_text,
                source="heartbeat",
                session_id=journal_session_id,
                turn_id=heartbeat.run_id,
                task_id=task_id,
                model=model,
                platform=platform,
            )
            if not recorded:
                self._finalize_heartbeat_decision(heartbeat, "suppressed_journal_failure")
                return "[SILENT]"
            if structured is not None:
                return (
                    str(structured.get("notification_text") or "").strip()
                    if structured.get("notify")
                    else "[SILENT]"
                )
            silent, visible = strip_heartbeat_ack(
                self._delivery_text(response_text), self.config.heartbeat_ack_max_chars
            )
            return "[SILENT]" if silent else visible
        if not task_id:
            try:
                self.engine.record_decision(
                    "silent",
                    "Fail-closed: scheduled cycle ended without calling tick",
                )
            except Exception as exc:
                logger.warning("conscious-agency missing-tick decision failed: %s", exc)
            output = "[SILENT]"
            self._record_subjective_output(
                output,
                source="heartbeat",
                session_id=journal_session_id,
                turn_id=heartbeat.run_id,
                model=model,
                platform=platform,
            )
            return output
        with self._cycle_lock:
            committed = self._active_cycles.get(task_id, {}).get("committed_output")
        if not committed:
            self._fail_closed_cycle(
                task_id,
                "Fail-closed: model ended scheduled cycle without record_decision",
            )
            output = "[SILENT]"
            self._record_subjective_output(
                output,
                source="heartbeat",
                session_id=journal_session_id,
                turn_id=heartbeat.run_id,
                task_id=task_id,
                model=model,
                platform=platform,
            )
            return output
        self.end_cycle(task_id)
        output = str(committed)
        committed_silent = output.strip().casefold() in {"", "[silent]", "heartbeat_ok"}
        structured_valid = bool(
            structured is not None
            and (
                (committed_silent and not structured.get("notify"))
                or (
                    not committed_silent
                    and structured.get("notify")
                    and str(structured.get("notification_text") or "").strip() == output.strip()
                )
            )
        )
        if not structured_valid:
            output = "[SILENT]"
            self._finalize_heartbeat_decision(heartbeat, "suppressed_contract")
        recorded = self._record_subjective_output(
            output,
            source="heartbeat",
            session_id=journal_session_id,
            turn_id=heartbeat.run_id,
            task_id=task_id,
            model=model,
            platform=platform,
        )
        if not recorded:
            self._finalize_heartbeat_decision(heartbeat, "suppressed_journal_failure")
        return output if recorded else "[SILENT]"

    def pre_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        task_id: str = "",
        session_id: str = "",
        **_: Any,
    ) -> dict[str, str] | None:
        if (
            current_heartbeat_turn() is not None
            and heartbeat_response() is not None
            and tool_name != "heartbeat_respond"
        ):
            return {
                "action": "block",
                "message": "Heartbeat decision already recorded; end the turn now.",
            }
        if self._expressive_subjective_heartbeat() and current_heartbeat_turn() is not None:
            return {
                "action": "block",
                "message": "Expressive Agency heartbeat runs without tools or research.",
            }
        if (
            current_heartbeat_turn() is not None
            and tool_name not in {"conscious_agency", "heartbeat_respond"}
            and not self.config.educational_allow_heartbeat_tools
        ):
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
            if _is_cron_session(session_id):
                return
            self.store.add_event(
                "tool_call",
                session_id=session_id,
                task_id=task_id,
                summary=tool_name[:200],
                metadata={
                    "duration_ms": int(duration_ms or 0),
                    "failed": _tool_result_failed(result),
                },
            )
        except Exception as exc:
            logger.debug("conscious-agency post_tool_call failed: %s", exc)

    def pre_llm_call(
        self,
        session_id: str = "",
        user_message: str = "",
        platform: str = "",
        task_id: str = "",
        turn_id: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> dict[str, str] | None:
        try:
            current_user_turn = False
            heartbeat = current_heartbeat_turn()
            if _is_cron_session(session_id):
                return None
            if heartbeat is not None:
                task = self._task_for_session(session_id)
                if task:
                    self._fail_closed_cycle(
                        task,
                        "Fail-closed: output transform was not applied before post_llm_call",
                    )
                self.store.add_event(
                    "heartbeat_turn_started",
                    session_id=session_id,
                    task_id=task_id,
                    summary="Native heartbeat agent turn started",
                    metadata={"run_id": heartbeat.run_id},
                )
            else:
                current_user_turn = begin_llm_turn(
                    session_id=session_id,
                    platform=platform,
                    user_message=user_message,
                    turn_id=turn_id,
                    kwargs=kwargs,
                )
            if (
                current_user_turn
                and _is_user_session(session_id, platform)
                and user_message.strip()
            ):
                self.engine.record_user_turn(
                    user_message,
                    session_id=session_id,
                    task_id=task_id,
                    platform=platform,
                )
            if (
                self.config.inject_context
                and self.config.enabled
                and (heartbeat is not None or current_user_turn)
            ):
                context_session_id = (
                    heartbeat.target_session_id
                    if heartbeat is not None and heartbeat.target_session_id
                    else session_id
                )
                agency_context = self.engine.context_block(
                    current_user_turn=current_user_turn,
                    unrestricted_heartbeat=(
                        heartbeat is not None and self._unrestricted_subjective_heartbeat()
                    ),
                    model_id=model,
                    session_id=context_session_id,
                    source="heartbeat" if heartbeat is not None else "conversation",
                )
                if heartbeat is not None:
                    policy: list[str] = [heartbeat.prompt] if heartbeat.prompt else []
                    if not self.config.educational_allow_uncommitted_output:
                        policy.append('Call conscious_agency with action="tick" first.')
                        if not self.config.educational_bypass_proactive_gates:
                            policy.append(
                                "Notify only when speak_eligible; otherwise use heartbeat_respond "
                                "with notify=false."
                            )
                        policy.append(
                            "Call record_decision before heartbeat_respond; notification_text must "
                            "equal the committed delivery_text."
                        )
                    if not self.config.educational_allow_heartbeat_tools:
                        policy.append(
                            "Do not call tools other than conscious_agency and heartbeat_respond."
                        )
                    return {
                        "context": "\n".join(policy + ([agency_context] if agency_context else []))
                    }
                return {"context": agency_context}
        except Exception as exc:
            logger.warning("conscious-agency pre_llm_call failed: %s", exc)
        return None

    def post_llm_call(
        self,
        session_id: str = "",
        assistant_response: str = "",
        platform: str = "",
        task_id: str = "",
        turn_id: str = "",
        model: str = "",
        **_: Any,
    ) -> None:
        try:
            if current_heartbeat_turn() is not None:
                heartbeat = current_heartbeat_turn()
                self.store.add_event(
                    "heartbeat_turn_finished",
                    session_id=session_id,
                    task_id=task_id,
                    summary="Native heartbeat agent turn finished",
                    metadata={
                        "run_id": heartbeat.run_id if heartbeat is not None else "",
                        "message_chars": len(assistant_response),
                    },
                )
            elif _is_user_session(session_id, platform) and should_capture_current_turn(turn_id):
                self._record_subjective_output(
                    assistant_response,
                    source="conversation",
                    session_id=session_id,
                    turn_id=turn_id,
                    task_id=task_id,
                    model=model,
                    platform=platform,
                )
                self.engine.record_assistant_turn(
                    assistant_response,
                    session_id=session_id,
                    task_id=task_id,
                    platform=platform,
                )
            self.store.prune_events()
        except Exception as exc:
            logger.debug("conscious-agency post_llm_call failed: %s", exc)
        finally:
            if not _is_cron_session(session_id):
                finish_llm_turn(turn_id)

    def session_event(
        self, kind: str, session_id: str = "", platform: str = "", **kwargs: Any
    ) -> None:
        try:
            if _is_cron_session(session_id) or not _is_user_session(session_id, platform):
                return
            metadata = {
                key: value
                for key, value in kwargs.items()
                if key in {"completed", "interrupted", "model"}
                and isinstance(value, (str, bool, int))
            }
            heartbeat = current_heartbeat_turn()
            if heartbeat is not None:
                metadata["run_id"] = heartbeat.run_id
            self.store.add_event(
                kind,
                session_id=session_id or "",
                platform=platform or "",
                summary=kind.replace("_", " "),
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("conscious-agency session hook failed: %s", exc)
