"""Hermes tool schema."""

CONSCIOUS_AGENCY_SCHEMA = {
    "name": "conscious_agency",
    "description": (
        "Manage persistent focus, intentions, questions, reflections, and observations. Use it "
        "only for persistent changes or explicit state requests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Operation to perform.",
                "enum": [
                    "status",
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
                "description": "Maximum conversational initiative for this intention.",
            },
            "due_at": {"type": "string", "description": "Optional ISO-8601 deadline."},
            "focus": {"type": "string"},
            "reason": {"type": "string", "description": "Decision, focus, or pause reason."},
            "question": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "Reflection summary.",
            },
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
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["action"],
    },
}


HEARTBEAT_RESPONSE_SCHEMA = {
    "name": "heartbeat_respond",
    "description": (
        "Complete the current native heartbeat. Use notify=false for no user interruption. "
        "Use notify=true with notification_text only when the user should receive a message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "notify": {"type": "boolean"},
            "notification_text": {
                "type": "string",
                "description": "Exact user-visible message when notify is true.",
            },
        },
        "required": ["notify"],
    },
}
