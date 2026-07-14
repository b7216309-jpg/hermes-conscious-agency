"""Hermes tool schema."""

CONSCIOUS_AGENCY_SCHEMA = {
    "name": "conscious_agency",
    "description": (
        "Read or update Hermes' persistent agency workspace: focus, intentions, open questions, "
        "reflections, and bounded proactive decisions. Use only for meaningful state changes, not "
        "routine narration. It cannot authorize external actions, change its permissions, or "
        "resume itself after a pause. During an agency cron cycle, call action='tick' first and "
        "obey its gates. Required fields by action: add_reflection requires summary; "
        "record_decision requires decision and reason, plus message when decision is speak; "
        "add_intention requires title; update_intention and resolve_question require id; "
        "set_focus requires focus; add_question requires question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Operation to perform; provide every action-specific required field."
                ),
                "enum": [
                    "status",
                    "snapshot",
                    "recent_events",
                    "list_intentions",
                    "add_intention",
                    "update_intention",
                    "set_focus",
                    "clear_focus",
                    "add_question",
                    "resolve_question",
                    "add_reflection",
                    "add_self_observation",
                    "tick",
                    "record_decision",
                    "pause",
                ],
            },
            "id": {"type": "string", "description": "Intention or question ID."},
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "status": {
                "type": "string",
                "enum": ["active", "blocked", "completed", "cancelled", "all"],
            },
            "autonomy": {
                "type": "string",
                "enum": ["reflect", "propose", "message"],
                "description": (
                    "Maximum conversational initiative for this intention; never grants "
                    "external-action permission."
                ),
            },
            "due_at": {"type": "string", "description": "Optional ISO-8601 deadline."},
            "focus": {"type": "string"},
            "reason": {
                "type": "string",
                "description": (
                    "Required for record_decision and pause; explain the concrete basis."
                ),
            },
            "question": {"type": "string"},
            "kind": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "Required for add_reflection; a concise, new, useful conclusion.",
            },
            "insight": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "observation": {"type": "string"},
            "decision": {
                "type": "string",
                "enum": ["silent", "speak"],
                "description": "Required for record_decision.",
            },
            "message": {
                "type": "string",
                "description": "Required for a speak decision; exact proposed delivery text.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["action"],
    },
}
