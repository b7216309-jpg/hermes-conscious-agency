"""Hermes Conscious Agency plugin registration."""

from __future__ import annotations

from copy import deepcopy

if __package__:  # Hermes loads this as a package.
    from .agency.cli import cli_command, register_cli, slash_command
    from .agency.heartbeat import arm_gateway_integration
    from .agency.runtime import AgencyRuntime
    from .agency.schemas import CONSCIOUS_AGENCY_SCHEMA, HEARTBEAT_RESPONSE_SCHEMA
else:  # Direct test/dev import of a plugin-root __init__.py.
    from agency.cli import cli_command, register_cli, slash_command
    from agency.heartbeat import arm_gateway_integration
    from agency.runtime import AgencyRuntime
    from agency.schemas import CONSCIOUS_AGENCY_SCHEMA, HEARTBEAT_RESPONSE_SCHEMA


def register(ctx) -> None:
    runtime = AgencyRuntime()
    schema = deepcopy(CONSCIOUS_AGENCY_SCHEMA)
    if runtime._expressive_subjective_heartbeat():
        actions = schema["parameters"]["properties"]["action"]["enum"]
        schema["parameters"]["properties"]["action"]["enum"] = [
            action for action in actions if action not in {"tick", "record_decision"}
        ]
    ctx.register_tool(
        name="conscious_agency",
        toolset="conscious_agency",
        schema=schema,
        handler=runtime.tool_handler,
        emoji="🧭",
    )
    ctx.register_tool(
        name="heartbeat_respond",
        toolset="conscious_agency",
        schema=deepcopy(HEARTBEAT_RESPONSE_SCHEMA),
        handler=runtime.heartbeat_handler,
        emoji="💓",
    )
    ctx.register_hook("pre_gateway_dispatch", runtime.pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", runtime.pre_llm_call)
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("llm_request", runtime.llm_request)
    ctx.register_hook("transform_llm_output", runtime.transform_llm_output)
    ctx.register_hook("post_llm_call", runtime.post_llm_call)
    ctx.register_hook("pre_tool_call", runtime.pre_tool_call)
    ctx.register_hook("post_tool_call", runtime.post_tool_call)
    ctx.register_hook("on_session_start", lambda **kw: runtime.session_event("session_start", **kw))
    ctx.register_hook("on_session_end", lambda **kw: runtime.session_event("session_end", **kw))
    ctx.register_hook(
        "on_session_finalize", lambda **kw: runtime.session_event("session_finalize", **kw)
    )
    ctx.register_hook("on_session_reset", lambda **kw: runtime.session_event("session_reset", **kw))
    ctx.register_command(
        "agency", handler=slash_command, description="Inspect or control persistent agency state."
    )
    ctx.register_cli_command(
        name="conscious-agency",
        help="Inspect and operate the Conscious Agency plugin",
        setup_fn=register_cli,
        handler_fn=cli_command,
        description="Persistent self-model, intentions, reflection, and bounded initiative.",
    )
    arm_gateway_integration(runtime)
