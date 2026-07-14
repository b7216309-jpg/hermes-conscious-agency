"""Hermes Conscious Agency plugin registration."""

from __future__ import annotations

if __package__:  # Hermes loads this as a package.
    from .agency.cli import cli_command, register_cli, slash_command
    from .agency.runtime import AgencyRuntime
    from .agency.schemas import CONSCIOUS_AGENCY_SCHEMA
else:  # Direct test/dev import of a plugin-root __init__.py.
    from agency.cli import cli_command, register_cli, slash_command
    from agency.runtime import AgencyRuntime
    from agency.schemas import CONSCIOUS_AGENCY_SCHEMA


def register(ctx) -> None:
    runtime = AgencyRuntime()
    ctx.register_tool(
        name="conscious_agency",
        toolset="conscious_agency",
        schema=CONSCIOUS_AGENCY_SCHEMA,
        handler=runtime.tool_handler,
        emoji="🧭",
    )
    ctx.register_hook("pre_llm_call", runtime.pre_llm_call)
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
