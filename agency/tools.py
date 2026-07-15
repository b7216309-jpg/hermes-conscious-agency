"""Model-facing tool implementation."""

from __future__ import annotations

import json
from typing import Any

from .engine import AgencyEngine


def _result(value: Any) -> str:
    return json.dumps({"success": True, "result": value}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def handle_agency(engine: AgencyEngine, args: dict[str, Any], **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "status").strip().lower()
        limit = max(1, min(int(args.get("limit") or 20), 100))
        if action == "status":
            runtime = engine.runtime()
            return _result(
                {
                    "enabled": engine.config.enabled,
                    "database_path": str(engine.store.path),
                    "paused": bool(runtime.get("paused")),
                    "pause_reason": runtime.get("pause_reason", ""),
                    "proactive_messages_enabled": engine.config.allow_proactive_messages,
                    "focus": engine.workspace().get("focus", ""),
                    "active_intentions": len(engine.store.list_intentions("active", 100)),
                }
            )
        if action == "snapshot":
            return _result(engine.snapshot())
        if action == "recent_events":
            return _result(engine.store.recent_events(limit))
        if action == "list_intentions":
            return _result(engine.store.list_intentions(str(args.get("status") or "active"), limit))
        if action == "add_intention":
            title = str(args.get("title") or "").strip()
            if not title:
                return _error("title is required")
            return _result(
                engine.store.add_intention(
                    title,
                    rationale=str(args.get("rationale") or ""),
                    priority=int(args.get("priority") or 50),
                    autonomy=str(args.get("autonomy") or "propose"),
                    due_at=args.get("due_at"),
                    source="agent",
                )
            )
        if action == "update_intention":
            item_id = str(args.get("id") or "")
            if not item_id:
                return _error("id is required")
            item = engine.store.update_intention(
                item_id,
                status=args.get("status"),
                priority=args.get("priority"),
                title=args.get("title"),
                rationale=args.get("rationale"),
                due_at=args.get("due_at") if "due_at" in args else None,
            )
            return _result(item) if item else _error("intention not found")
        if action == "set_focus":
            focus = str(args.get("focus") or args.get("title") or "")
            if not focus.strip():
                return _error("focus is required")
            return _result(engine.set_focus(focus, str(args.get("reason") or "")))
        if action == "clear_focus":
            return _result(engine.set_focus("", str(args.get("reason") or "")))
        if action == "add_question":
            question = str(args.get("question") or "")
            if not question.strip():
                return _error("question is required")
            return _result(engine.add_question(question))
        if action == "resolve_question":
            item_id = str(args.get("id") or "")
            return _result({"resolved": engine.resolve_question(item_id)})
        if action == "add_reflection":
            summary = str(args.get("summary") or "")
            if not summary.strip():
                return _error("summary is required")
            return _result(
                engine.store.add_reflection(
                    str(args.get("kind") or "general"),
                    summary,
                    insight=str(args.get("insight") or ""),
                    confidence=float(
                        args.get("confidence") if args.get("confidence") is not None else 0.5
                    ),
                )
            )
        if action == "add_self_observation":
            observation = str(args.get("observation") or "")
            if not observation.strip():
                return _error("observation is required")
            return _result(engine.add_self_observation(observation))
        if action == "tick":
            return _result(engine.evaluate_tick())
        if action == "record_decision":
            decision = str(args.get("decision") or "")
            value = engine.record_decision(
                decision,
                str(args.get("reason") or ""),
                message=str(args.get("message") or ""),
                intention_id=str(args.get("id") or "") or None,
            )
            return _result(value)
        if action == "pause":
            return _result(engine.pause(str(args.get("reason") or "Agent requested safety pause")))
        return _error(f"unknown action: {action}")
    except PermissionError as exc:
        return _error(str(exc))
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"agency operation failed: {type(exc).__name__}: {exc}")
