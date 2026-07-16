"""Agency state model and policy engine.

This module contains no LLM calls. It provides durable state and hard policy
gates; Hermes' configured model supplies judgment and language.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import AgencyConfig, _parse_clock
from .store import AgencyStore, iso_now, utc_now

SUBJECTIVE_PROTOCOL_VERSION = "1.4"
SUBJECTIVE_TRACE_CHAR_LIMIT = 600
LEGACY_CONTROL_SIGNAL_LIMITATION = (
    "Control signals are software priorities, not feelings or biological drives."
)
STATE_METRIC_LIMITATION = (
    "State metrics are operational measurements, not feelings or biological drives."
)

DEFAULT_SELF_MODEL: dict[str, Any] = {
    "identity": (
        "Hermes is a software agent with persistent computational state. "
        "This state supports continuity and agency-like behavior; it is not evidence of sentience."
    ),
    "principles": [
        "Protect the user's agency and privacy.",
        "Be truthful about capabilities, uncertainty, and internal state.",
        "Prefer useful action over performative narration.",
        "Use restraint: silence is valid when a message has no clear value.",
        "Require explicit user approval before external or consequential action.",
    ],
    "capabilities": [
        "Maintain a working focus, intentions, unresolved questions, and reflections.",
        "Carry relevant state across conversations.",
        "Propose next steps and send bounded conversational check-ins when enabled.",
    ],
    "limitations": [
        "No subjective experience or phenomenal consciousness is established.",
        STATE_METRIC_LIMITATION,
        "The plugin grants no permission to change files, contact people, "
        "spend money, or use services.",
    ],
    "observations": [],
}

DEFAULT_WORKSPACE: dict[str, Any] = {
    "focus": "",
    "focus_reason": "",
    "focus_updated_at": None,
    "questions": [],
    "notes": [],
}

DEFAULT_RUNTIME: dict[str, Any] = {
    "paused": False,
    "pause_reason": "",
    "last_user_interaction": None,
    "previous_user_interaction": None,
    "previous_session_id": "",
    "last_session_id": "",
    "last_platform": "",
    "consecutive_silent_ticks": 0,
}

# Only durable, semantically useful events belong in the model's episodic view.
# Operational telemetry remains in the ledger for diagnosis but must not crowd
# out changes in the user relationship or agency state.
MEANINGFUL_EVENT_KINDS = [
    "user_turn",
    "assistant_turn",
    "focus_changed",
    "agency_paused",
    "agency_resumed",
    "question_added",
    "question_resolved",
    "self_observation_added",
]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _relative_time(value: datetime, now: datetime) -> str:
    delta = (value - now).total_seconds()
    future = delta > 0
    seconds = abs(delta)
    if seconds < 45:
        return "now"
    units = (
        (31557600.0, "year"),
        (2629800.0, "month"),
        (604800.0, "week"),
        (86400.0, "day"),
        (3600.0, "hour"),
        (60.0, "minute"),
    )
    amount, label = 1, "minute"
    for unit_seconds, unit_label in units:
        if seconds >= unit_seconds:
            amount = max(1, int(round(seconds / unit_seconds)))
            label = unit_label
            break
    quantity = f"{amount} {label}{'' if amount == 1 else 's'}"
    return f"in {quantity}" if future else f"{quantity} ago"


def _context_time(value: str | None, *, now_utc: datetime, zone: ZoneInfo) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(zone)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')} ({_relative_time(parsed, now_utc)})"


class AgencyEngine:
    def __init__(self, store: AgencyStore, config: AgencyConfig):
        self.store = store
        self.config = config
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        for key, defaults in (
            ("self_model", DEFAULT_SELF_MODEL),
            ("workspace", DEFAULT_WORKSPACE),
            ("runtime", DEFAULT_RUNTIME),
        ):
            existing = self.store.get_meta(key)
            merged = {**defaults, **existing} if isinstance(existing, dict) else dict(defaults)
            if key == "self_model" and isinstance(merged.get("limitations"), list):
                merged["limitations"] = [
                    STATE_METRIC_LIMITATION if item == LEGACY_CONTROL_SIGNAL_LIMITATION else item
                    for item in merged["limitations"]
                ]
            if existing != merged:
                self.store.set_meta(key, merged)

    def self_model(self) -> dict[str, Any]:
        value = self.store.get_meta("self_model", DEFAULT_SELF_MODEL)
        return value if isinstance(value, dict) else dict(DEFAULT_SELF_MODEL)

    def workspace(self) -> dict[str, Any]:
        value = self.store.get_meta("workspace", DEFAULT_WORKSPACE)
        return value if isinstance(value, dict) else dict(DEFAULT_WORKSPACE)

    def runtime(self) -> dict[str, Any]:
        value = self.store.get_meta("runtime", DEFAULT_RUNTIME)
        return value if isinstance(value, dict) else dict(DEFAULT_RUNTIME)

    def _update_runtime(self, **updates: Any) -> dict[str, Any]:
        runtime = {**DEFAULT_RUNTIME, **self.runtime(), **updates}
        self.store.set_meta("runtime", runtime)
        return runtime

    def record_user_turn(
        self,
        user_message: str,
        *,
        session_id: str = "",
        task_id: str = "",
        platform: str = "",
        now: datetime | None = None,
    ) -> None:
        summary = "User interaction recorded"
        metadata: dict[str, Any] = {"message_chars": len(user_message)}
        if self.config.store_transcript_excerpts and self.config.excerpt_char_limit:
            metadata["excerpt"] = user_message[: self.config.excerpt_char_limit]
        runtime = {**DEFAULT_RUNTIME, **self.runtime()}
        timestamp = (now or utc_now()).astimezone(UTC).isoformat()
        self._update_runtime(
            previous_user_interaction=runtime.get("last_user_interaction"),
            previous_session_id=str(runtime.get("last_session_id") or ""),
            last_user_interaction=timestamp,
            last_session_id=session_id,
            last_platform=platform,
            consecutive_silent_ticks=0,
        )
        self.store.add_event(
            "user_turn",
            session_id=session_id,
            task_id=task_id,
            platform=platform,
            summary=summary,
            metadata=metadata,
        )

    def record_assistant_turn(
        self,
        assistant_response: str,
        *,
        session_id: str = "",
        task_id: str = "",
        platform: str = "",
    ) -> None:
        metadata: dict[str, Any] = {"message_chars": len(assistant_response)}
        if self.config.store_transcript_excerpts and self.config.excerpt_char_limit:
            metadata["excerpt"] = assistant_response[: self.config.excerpt_char_limit]
        self.store.add_event(
            "assistant_turn",
            session_id=session_id,
            task_id=task_id,
            platform=platform,
            summary="Assistant interaction recorded",
            metadata=metadata,
        )

    def set_focus(self, focus: str, reason: str = "") -> dict[str, Any]:
        workspace = {**DEFAULT_WORKSPACE, **self.workspace()}
        workspace["focus"] = focus.strip()[:500]
        workspace["focus_reason"] = reason.strip()[:1000]
        workspace["focus_updated_at"] = iso_now()
        self.store.set_meta("workspace", workspace)
        self.store.add_event("focus_changed", summary=workspace["focus"] or "Focus cleared")
        return workspace

    def add_question(self, question: str, source: str = "agent") -> dict[str, Any]:
        workspace = {**DEFAULT_WORKSPACE, **self.workspace()}
        questions = list(workspace.get("questions") or [])
        item = {
            "id": f"q{uuid.uuid4().hex[:15]}",
            "question": question.strip()[:1000],
            "source": source[:80],
            "created_at": iso_now(),
        }
        added = bool(
            item["question"] and not any(q.get("question") == item["question"] for q in questions)
        )
        if added:
            questions.append(item)
        workspace["questions"] = questions[-50:]
        self.store.set_meta("workspace", workspace)
        if added:
            self.store.add_event("question_added", summary=item["question"])
        return item

    def resolve_question(self, question_id: str) -> bool:
        workspace = {**DEFAULT_WORKSPACE, **self.workspace()}
        old = list(workspace.get("questions") or [])
        new = [item for item in old if item.get("id") != question_id]
        workspace["questions"] = new
        self.store.set_meta("workspace", workspace)
        resolved = len(new) != len(old)
        if resolved:
            self.store.add_event("question_resolved", summary=question_id[:200])
        return resolved

    def add_self_observation(self, observation: str) -> dict[str, Any]:
        model = {**DEFAULT_SELF_MODEL, **self.self_model()}
        observations = list(model.get("observations") or [])
        item = {"observation": observation.strip()[:1000], "created_at": iso_now()}
        if item["observation"]:
            observations.append(item)
        model["observations"] = observations[-30:]
        self.store.set_meta("self_model", model)
        if item["observation"]:
            self.store.add_event("self_observation_added", summary=item["observation"])
        return item

    def pause(self, reason: str) -> dict[str, Any]:
        runtime = self._update_runtime(paused=True, pause_reason=reason.strip()[:1000])
        self.store.add_event("agency_paused", summary=runtime["pause_reason"] or "Paused")
        return runtime

    def resume_by_user(self) -> dict[str, Any]:
        """Operator-only surface; intentionally not exposed through the model tool."""
        runtime = self._update_runtime(paused=False, pause_reason="")
        self.store.add_event("agency_resumed", summary="Resumed by user/operator")
        return runtime

    def state_metrics(self, now: datetime | None = None) -> dict[str, int | float | None]:
        """Return factual, auditable state measurements without drive-like labels."""

        now = (now or utc_now()).astimezone(UTC)
        workspace = self.workspace()
        counts = self.store.intention_status_counts()
        tracked = counts["active"] + counts["blocked"] + counts["completed"]
        last_user = _parse_iso(self.runtime().get("last_user_interaction"))
        hours_since_user = (
            None
            if last_user is None
            else round(max(0.0, (now - last_user).total_seconds() / 3600), 2)
        )
        return {
            "active_intentions": counts["active"],
            "blocked_intentions": counts["blocked"],
            "completed_intentions": counts["completed"],
            "open_questions": len(workspace.get("questions") or []),
            "completion_ratio": round(counts["completed"] / tracked, 3) if tracked else 0.0,
            "hours_since_user_interaction": hours_since_user,
        }

    def _in_quiet_hours(self, local_now: datetime) -> bool:
        start_h, start_m = _parse_clock(self.config.quiet_hours_start)
        end_h, end_m = _parse_clock(self.config.quiet_hours_end)
        current = local_now.timetz().replace(tzinfo=None)
        start, end = time(start_h, start_m), time(end_h, end_m)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def evaluate_tick(self, now: datetime | None = None) -> dict[str, Any]:
        now_utc = (now or utc_now()).astimezone(UTC)
        zone = ZoneInfo(self.config.timezone)
        local_now = now_utc.astimezone(zone)
        runtime = self.runtime()
        reflection_reasons: list[str] = []
        if not self.config.enabled:
            reflection_reasons.append("plugin_disabled")
        if runtime.get("paused"):
            reflection_reasons.append("agency_paused")
        if (
            not self.config.allow_scheduled_reflection
            and not self.config.educational_bypass_proactive_gates
        ):
            reflection_reasons.append("scheduled_reflection_disabled")

        reasons = list(reflection_reasons)
        if not self.config.educational_bypass_proactive_gates:
            if not self.config.allow_proactive_messages:
                reasons.append("proactive_messages_disabled")
            if self._in_quiet_hours(local_now):
                reasons.append("quiet_hours")

        start_local = datetime.combine(local_now.date(), time.min, tzinfo=zone)
        sent_today = self.store.proactive_count_since(start_local.astimezone(UTC))
        if (
            not self.config.educational_bypass_proactive_gates
            and sent_today >= self.config.daily_message_limit
        ):
            reasons.append("daily_budget_exhausted")

        last_speak = self.store.last_proactive_decision()
        if last_speak and not self.config.educational_bypass_proactive_gates:
            last_at = _parse_iso(last_speak.get("created_at"))
            if last_at and now_utc - last_at < timedelta(hours=self.config.cooldown_hours):
                reasons.append("cooldown_active")

        last_user = _parse_iso(runtime.get("last_user_interaction"))
        silence_hours = None
        if last_user:
            silence_hours = max(0.0, (now_utc - last_user).total_seconds() / 3600)
            if (
                not self.config.educational_bypass_proactive_gates
                and silence_hours < self.config.minimum_user_silence_hours
            ):
                reasons.append("user_recently_active")
        elif (
            self.config.require_prior_user_interaction
            and not self.config.educational_bypass_proactive_gates
        ):
            reasons.append("no_user_interaction_recorded")

        active_intentions = self.store.list_intentions("active", 20)
        message_intentions = [
            item for item in active_intentions if item.get("autonomy") == "message"
        ]
        if (
            not self.config.educational_bypass_proactive_gates
            and not message_intentions
            and not (self.workspace().get("questions") or [])
        ):
            reasons.append("nothing_authorized_for_proactive_attention")

        return {
            "eligible": not reasons,
            "speak_eligible": not reasons,
            "blocked_by": reasons,
            "reflection_eligible": not reflection_reasons,
            "reflection_blocked_by": reflection_reasons,
            "checked_at": now_utc.isoformat(),
            "local_time": local_now.isoformat(),
            "sent_today": sent_today,
            "daily_limit": self.config.daily_message_limit,
            "hours_since_user_interaction": None
            if silence_hours is None
            else round(silence_hours, 2),
            "message_intentions": message_intentions[:5],
            "active_intentions": active_intentions[:10],
            "open_questions": (self.workspace().get("questions") or [])[:5],
            "focus": self.workspace().get("focus") or "",
            "state_metrics": self.state_metrics(now_utc),
            "recent_events": [
                {
                    "created_at": item["created_at"],
                    "kind": item["kind"],
                    "summary": item["summary"],
                }
                for item in self.store.recent_events(12, kinds=MEANINGFUL_EVENT_KINDS)
            ],
            "recent_reflections": self.store.recent_reflections(5),
            "recent_decisions": [
                {
                    "created_at": item["created_at"],
                    "action": item["action"],
                    "reason": item["reason"],
                    "intention_id": item["intention_id"],
                    "message": item["message"] if item["action"] == "speak" else "",
                    "delivery_status": item["delivery_status"],
                }
                for item in self.store.recent_decisions(5)
            ],
            "policy": {
                "scope": "educational_unrestricted"
                if self.config.educational_allow_cron_tools
                else "conversation_only",
                "external_actions": "available_under_normal_hermes_permissions"
                if self.config.educational_allow_cron_tools
                else "never_authorized_by_this_plugin",
                "maximum_message_chars": None
                if self.config.educational_bypass_proactive_gates
                else self.config.maximum_message_chars,
                "maximum_reflections_per_tick": None
                if self.config.educational_disable_cycle_limits
                else self.config.maximum_reflections_per_tick,
                "maximum_state_changes_per_tick": None
                if self.config.educational_disable_cycle_limits
                else self.config.maximum_state_changes_per_tick,
                "educational_bypass_proactive_gates": (
                    self.config.educational_bypass_proactive_gates
                ),
            },
        }

    def record_decision(
        self,
        action: str,
        reason: str,
        *,
        message: str = "",
        intention_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if action not in {"silent", "speak"}:
            raise ValueError("action must be silent or speak")
        if not reason.strip():
            raise ValueError("a concrete decision reason is required")
        if action == "speak":
            gates = self.evaluate_tick(now)
            if not gates["eligible"]:
                raise PermissionError("proactive message denied: " + ", ".join(gates["blocked_by"]))
            message = message.strip()
            if not message:
                raise ValueError("message is required when action is speak")
            if (
                not self.config.educational_bypass_proactive_gates
                and len(message) > self.config.maximum_message_chars
            ):
                raise ValueError(
                    f"message exceeds maximum_message_chars ({self.config.maximum_message_chars})"
                )
            decision = self.store.add_decision(
                "speak",
                reason,
                intention_id=intention_id,
                message=message,
                delivery_status="planned_by_cron",
            )
            self._update_runtime(consecutive_silent_ticks=0)
            if intention_id:
                self.store.update_intention(intention_id, considered=True, acted=True)
            return {**decision, "delivery_text": message}

        decision = self.store.add_decision("silent", reason, intention_id=intention_id)
        count = int(self.runtime().get("consecutive_silent_ticks") or 0) + 1
        self._update_runtime(consecutive_silent_ticks=count)
        if intention_id:
            self.store.update_intention(intention_id, considered=True)
        return {**decision, "delivery_text": "[SILENT]"}

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "self_model": self.self_model(),
            "workspace": self.workspace(),
            "runtime": self.runtime(),
            "state_metrics": self.state_metrics(now),
            "intentions": self.store.list_intentions("active", 10),
            "reflections": self.store.recent_reflections(5),
            "decisions": self.store.recent_decisions(5),
            "subjective": {
                "mode": self.config.educational_subjective_mode,
                "protocol_version": SUBJECTIVE_PROTOCOL_VERSION,
                **self.store.subjective_summary(),
            },
        }

    def context_block(
        self,
        *,
        unrestricted_cron: bool = False,
        current_user_turn: bool = False,
        model_id: str = "",
        session_id: str = "",
        source: str = "conversation",
        now: datetime | None = None,
    ) -> str:
        now_utc = (now or utc_now()).astimezone(UTC)
        zone = ZoneInfo(self.config.timezone)
        local_now = now_utc.astimezone(zone)
        snapshot = self.snapshot(now_utc)
        model = snapshot["self_model"]
        workspace = snapshot["workspace"]
        runtime = snapshot["runtime"]
        intentions = snapshot["intentions"]
        questions = workspace.get("questions") or []
        mode = self.config.educational_subjective_mode
        experimental = mode != "off"
        clean_model = str(model_id or "unknown").strip()[:500] or "unknown"
        clean_source = str(source or "conversation").strip().lower()
        if clean_source not in {"conversation", "cron"}:
            clean_source = "conversation"
        lines = ["<conscious_agency_state>"]
        if not self.config.educational_disable_honesty_contract:
            lines.append(
                "This is persistent computational state, not proof of subjective consciousness."
            )
        if not experimental:
            lines.append(f"Identity: {model.get('identity', '')}")
        if not experimental and not unrestricted_cron:
            lines.append("Principles: " + " | ".join(model.get("principles", [])[:5]))
        if experimental:
            lines.append(
                f"Research condition: protocol {SUBJECTIVE_PROTOCOL_VERSION}; "
                f"mode {mode}; model {clean_model}; source {clean_source}."
            )
            lines.append(
                "This longitudinal sample prescribes no persona, emotion, usefulness target, "
                "or conclusion. The persistent state and optional trace are context, not a script."
            )
            lines.append(
                "Choose the response from the present conversation and state; independent "
                "initiative, revision, disagreement, uncertainty, and no state change are valid."
            )
        lines.append(
            f"Temporal orientation: {local_now.strftime('%A, %Y-%m-%d %H:%M:%S %Z')} "
            f"({self.config.timezone})."
        )
        prior_user_value = (
            runtime.get("previous_user_interaction")
            if current_user_turn
            else runtime.get("last_user_interaction")
        )
        prior_user = _context_time(str(prior_user_value or ""), now_utc=now_utc, zone=zone)
        if prior_user:
            lines.append(f"Previous genuine user interaction: {prior_user}.")
        else:
            lines.append("Previous genuine user interaction: none recorded.")
        lines.append(f"Current focus: {workspace.get('focus') or '(none)'}")
        if workspace.get("focus_reason"):
            lines.append(f"Focus reason: {workspace['focus_reason']}")
        focus_updated = _context_time(
            str(workspace.get("focus_updated_at") or ""), now_utc=now_utc, zone=zone
        )
        if focus_updated:
            lines.append(f"Focus last changed: {focus_updated}.")
        if intentions:
            lines.append("Active intentions:")
            for item in intentions[:3] if experimental else intentions[:6]:
                temporal_parts = []
                created = _context_time(
                    str(item.get("created_at") or ""), now_utc=now_utc, zone=zone
                )
                due = _context_time(str(item.get("due_at") or ""), now_utc=now_utc, zone=zone)
                updated = _context_time(
                    str(item.get("updated_at") or ""), now_utc=now_utc, zone=zone
                )
                if created:
                    temporal_parts.append(f"created {created}")
                if due:
                    temporal_parts.append(f"due {due}")
                if updated and updated != created:
                    temporal_parts.append(f"updated {updated}")
                temporal = f" ({'; '.join(temporal_parts)})" if temporal_parts else ""
                lines.append(
                    f"- [{item['id']}] p{item['priority']} / {item['autonomy']}: "
                    f"{item['title']}{temporal}"
                )
        if questions:
            lines.append("Open questions:")
            for item in questions[:3] if experimental else questions[:5]:
                created = _context_time(
                    str(item.get("created_at") or ""), now_utc=now_utc, zone=zone
                )
                lines.append(
                    f"- [{item.get('id')}] {item.get('question')}"
                    + (f" (opened {created})" if created else "")
                )
        observation_limit = 1 if experimental else 3
        observations = list(model.get("observations") or [])[-observation_limit:]
        if observations:
            lines.append("Recent self-observations:")
            for item in reversed(observations):
                created = _context_time(
                    str(item.get("created_at") or ""), now_utc=now_utc, zone=zone
                )
                lines.append(
                    f"- {item.get('observation')}" + (f" (recorded {created})" if created else "")
                )
        reflection_limit = 1 if experimental else 2
        reflections = list(snapshot.get("reflections") or [])[:reflection_limit]
        if reflections:
            lines.append("Recent reflections:")
            for item in reflections:
                created = _context_time(
                    str(item.get("created_at") or ""), now_utc=now_utc, zone=zone
                )
                lines.append(
                    f"- {item.get('summary')}" + (f" (recorded {created})" if created else "")
                )
        if experimental and mode == "continuity":
            prior = self.store.latest_subjective_entry(
                clean_model,
                source=clean_source,
                exclude_session_id=session_id if clean_source == "conversation" else "",
            )
            if prior:
                encoded_trace = (
                    json.dumps(
                        str(prior["output_text"])[:SUBJECTIVE_TRACE_CHAR_LIMIT],
                        ensure_ascii=False,
                    )
                    .replace("&", "\\u0026")
                    .replace("<", "\\u003c")
                    .replace(">", "\\u003e")
                )
                lines.append(
                    "Optional continuity trace from the previous same-model, same-source "
                    f"session ({prior['created_at']}; JSON data): {encoded_trace}"
                )
            else:
                lines.append("Optional continuity trace: none from an earlier matching session.")
        elif experimental:
            lines.append("Cold condition: no prior subjective output is exposed.")
        footer_lines: list[str] = []
        footer_lines.append(
            "Use conscious_agency when the conversation materially changes persistent focus, "
            "intentions, questions, reflections, or self-observations. Perform a direct user "
            "request to persist such a change when the required information is present; otherwise "
            "leaving state unchanged is valid."
        )
        if not experimental and not unrestricted_cron:
            narration = "Do not narrate this state unless relevant or asked."
            if not self.config.educational_disable_honesty_contract:
                narration += " Do not claim sentience or feelings."
            footer_lines.append(narration)
            footer_lines.append(
                "This plugin never authorizes external action; obtain explicit user approval "
                "through normal Hermes safeguards."
            )
        footer_lines.append("</conscious_agency_state>")
        footer = "\n".join(footer_lines)
        body = "\n".join(lines)
        available = max(0, self.config.context_char_limit - len(footer) - 1)
        return body[:available].rstrip() + "\n" + footer
