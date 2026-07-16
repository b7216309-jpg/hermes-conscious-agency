from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


class FakeContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.cli = {}
        self.middleware = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_middleware(self, name, handler):
        self.middleware[name] = handler

    def register_command(self, name, handler=None, **kwargs):
        self.commands[name] = handler or kwargs.get("handler")

    def register_cli_command(self, **kwargs):
        self.cli[kwargs["name"]] = kwargs


def load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "conscious_agency_test_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_registers_complete_hermes_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = load_plugin()
    context = FakeContext()
    plugin.register(context)
    assert set(context.tools) == {"conscious_agency"}
    assert set(context.hooks) == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "transform_llm_output",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
    }
    assert "agency" in context.commands
    assert "conscious-agency" in context.cli
    assert set(context.middleware) == {"llm_request"}


def test_expressive_subjective_mode_hides_cron_only_tool_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_subjective_mode: continuity\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_cron_tools: false\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    plugin = load_plugin()
    context = FakeContext()

    plugin.register(context)

    actions = context.tools["conscious_agency"]["schema"]["parameters"]["properties"]["action"][
        "enum"
    ]
    assert "tick" not in actions
    assert "record_decision" not in actions
    assert "set_focus" in actions
