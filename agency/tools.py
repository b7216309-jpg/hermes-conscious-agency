"""Model-facing tool implementation."""

from __future__ import annotations

import json
from typing import Any

from .engine import AgencyEngine


def _result(value: Any) -> str:
    return json.dumps({"success": True, "result": value}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _intention(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "status": item.get("status", ""),
        "priority": item.get("priority"),
        "autonomy": item.get("autonomy", ""),
        "due_at": item.get("due_at"),
    }


def handle_agency(engine: AgencyEngine, args: dict[str, Any], **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "status").strip().lower()
        limit = max(1, min(int(args.get("limit") or 20), 20))
        if action == "status":
            runtime = engine.runtime()
            return _result(
                {
                    "enabled": engine.config.enabled,
                    "paused": bool(runtime.get("paused")),
                    "pause_reason": runtime.get("pause_reason", ""),
                    "focus": engine.workspace().get("focus", ""),
                    "active_intentions": engine.store.intention_status_counts()["active"],
                }
            )
        if action == "snapshot":
            return _result(engine.snapshot())
        if action == "recent_events":
            return _result(engine.store.recent_events(limit))
        if action == "list_intentions":
            items = engine.store.list_intentions(str(args.get("status") or "active"), limit)
            return _result([_intention(item) for item in items])
        if action == "add_intention":
            title = str(args.get("title") or "").strip()
            if not title:
                return _error("title is required")
            return _result(
                _intention(
                    engine.store.add_intention(
                        title,
                        rationale=str(args.get("rationale") or ""),
                        priority=int(args.get("priority") or 50),
                        autonomy=str(args.get("autonomy") or "propose"),
                        due_at=args.get("due_at"),
                        source="agent",
                    )
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
            return _result(_intention(item)) if item else _error("intention not found")
        if action == "set_focus":
            focus = str(args.get("focus") or args.get("title") or "")
            if not focus.strip():
                return _error("focus is required")
            workspace = engine.set_focus(focus, str(args.get("reason") or ""))
            return _result({"focus": workspace.get("focus", "")})
        if action == "clear_focus":
            engine.set_focus("", str(args.get("reason") or ""))
            return _result({"focus": ""})
        if action == "add_question":
            question = str(args.get("question") or "")
            if not question.strip():
                return _error("question is required")
            item = engine.add_question(question)
            return _result({"id": item.get("id"), "question": item.get("question", "")})
        if action == "resolve_question":
            item_id = str(args.get("id") or "")
            return _result({"resolved": engine.resolve_question(item_id)})
        if action == "add_reflection":
            summary = str(args.get("summary") or "")
            if not summary.strip():
                return _error("summary is required")
            item = engine.store.add_reflection(
                str(args.get("kind") or "general"),
                summary,
                insight=str(args.get("insight") or ""),
                confidence=float(
                    args.get("confidence") if args.get("confidence") is not None else 0.5
                ),
            )
            return _result({"id": item.get("id"), "summary": item.get("summary", "")})
        if action == "add_self_observation":
            observation = str(args.get("observation") or "")
            if not observation.strip():
                return _error("observation is required")
            item = engine.add_self_observation(observation)
            return _result({"id": item.get("id"), "observation": item.get("observation", "")})
        if action == "tick":
            return _result(engine.model_tick())
        if action == "record_decision":
            decision = str(args.get("decision") or "")
            value = engine.record_decision(
                decision,
                str(args.get("reason") or ""),
                message=str(args.get("message") or ""),
                intention_id=str(args.get("id") or "") or None,
            )
            return _result(
                {
                    "id": value.get("id"),
                    "decision": value.get("action"),
                    "delivery_text": value.get("delivery_text"),
                }
            )
        if action == "pause":
            runtime = engine.pause(str(args.get("reason") or "Agent requested safety pause"))
            return _result({"paused": runtime.get("paused"), "reason": runtime.get("pause_reason")})
        return _error(f"unknown action: {action}")
    except PermissionError as exc:
        return _error(str(exc))
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"agency operation failed: {type(exc).__name__}: {exc}")
