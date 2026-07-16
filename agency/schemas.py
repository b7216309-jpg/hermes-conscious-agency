"""Hermes tool schema."""

CONSCIOUS_AGENCY_SCHEMA = {
    "name": "conscious_agency",
    "description": (
        "Inspect or materially update Hermes' persistent focus, intentions, open questions, "
        "reflections, self-observations, and proactive decisions. Use it when the conversation "
        "materially changes that state, and perform a direct user request to persist a state "
        "change when the required fields are present. Leaving state unchanged is valid otherwise; "
        "do not call this tool for routine narration. A bounded cron cycle may require tick and "
        "record_decision according to its injected cycle instructions. This tool cannot raise "
        "permissions, authorize external action, or resume a paused plugin."
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
