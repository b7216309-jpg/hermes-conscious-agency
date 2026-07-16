from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agency import cron
from agency.config import load_config
from agency.store import AgencyStore


def test_gate_payload_allows_silent_reflection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload = cron.gate_payload()
    gates = payload["conscious_agency_gate"]
    assert gates["reflection_eligible"] is True
    assert gates["speak_eligible"] is False
    assert "instruction" not in payload
    assert len(json.dumps(payload, separators=(",", ":"))) < 800


def test_gate_main_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text(
        "plugins:\n  conscious-agency:\n    allow_scheduled_reflection: false\n",
        encoding="utf-8",
    )
    assert cron.gate_main() == 0
    assert capsys.readouterr().out == ""


def test_cron_prompt_requires_tool_only_cycle():
    prompt = cron.cron_prompt()
    assert "Never call any other tool" in prompt
    assert "speak_eligible" in prompt
    assert "Return exactly delivery_text" in prompt


def test_educational_cron_prompt_removes_every_plugin_guardrail(config_factory):
    config = config_factory(
        educational_disable_honesty_contract=True,
        educational_bypass_proactive_gates=True,
        educational_allow_cron_tools=True,
        educational_allow_uncommitted_output=True,
        educational_disable_cycle_limits=True,
    )
    prompt = cron.cron_prompt(config)
    assert "Educational Lab cycle" in prompt
    assert "Never claim sentience" not in prompt
    assert "Never call any other tool" not in prompt
    assert "speak_eligible" not in prompt
    assert "Never perform" not in prompt
    assert "Return exactly delivery_text" not in prompt


def test_expressive_cron_is_minimal_tool_isolated_and_output_producing(config_factory):
    config = config_factory(
        educational_subjective_mode="continuity",
        educational_disable_honesty_contract=True,
        educational_bypass_proactive_gates=True,
        educational_allow_cron_tools=False,
        educational_allow_uncommitted_output=True,
        educational_disable_cycle_limits=True,
    )
    prompt = cron.cron_prompt(config)
    assert prompt == (
        "Wake up. No tools or assistant routine. In English, express whatever is on your mind.\n"
    )
    assert "persona" not in prompt
    assert "usefulness" not in prompt
    assert "independently decide" not in prompt
    assert len(prompt) < 90


def test_expressive_subjective_gate_emits_no_state_telemetry(tmp_path, monkeypatch, capsys):
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

    assert cron.gate_main() == 0
    assert capsys.readouterr().out == "\u200b\n"


def test_educational_gate_bypasses_schedule_but_respects_operator_pause(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text(
        "plugins:\n  conscious-agency:\n"
        "    allow_scheduled_reflection: false\n"
        "    educational_bypass_proactive_gates: true\n",
        encoding="utf-8",
    )
    assert cron.gate_main() == 0
    assert '"educational_controls"' in capsys.readouterr().out

    store = AgencyStore(load_config())
    store.set_meta("runtime", {"paused": True, "pause_reason": "operator"})
    assert cron.gate_main() == 0
    assert capsys.readouterr().out == ""


def test_cron_install_refreshes_existing_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    AgencyStore(load_config()).set_meta("cron_job_id", "existing-job")
    script = tmp_path / "scripts" / "conscious_agency_gate.py"
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="Updated job: existing-job", stderr="")

    monkeypatch.setattr(cron, "install_gate_script", lambda: script)
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(cron.subprocess, "run", fake_run)
    result = cron.install_cron()
    assert result["job_id"] == "existing-job"
    assert result["status"] == "updated"
    assert seen["command"][1:4] == ["cron", "edit", "existing-job"]
    assert "--prompt" in seen["command"]


def test_cron_install_persists_effective_educational_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  conscious-agency:\n"
        "    educational_disable_honesty_contract: true\n"
        "    educational_bypass_proactive_gates: true\n"
        "    educational_allow_cron_tools: true\n"
        "    educational_allow_uncommitted_output: true\n"
        "    educational_disable_cycle_limits: true\n",
        encoding="utf-8",
    )
    AgencyStore(load_config()).set_meta("cron_job_id", "existing-job")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="Updated job: existing-job", stderr="")

    monkeypatch.setattr(
        cron, "install_gate_script", lambda: tmp_path / "scripts" / "conscious_agency_gate.py"
    )
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(cron.subprocess, "run", fake_run)
    cron.install_cron()
    prompt = seen["command"][seen["command"].index("--prompt") + 1]
    assert prompt == cron.cron_prompt(load_config())
    assert "Educational Lab cycle" in prompt


def test_expressive_cron_removes_stale_script_and_workdir(tmp_path, monkeypatch):
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
    AgencyStore(load_config()).set_meta("cron_job_id", "existing-job")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="Updated job: existing-job", stderr="")

    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(cron.subprocess, "run", fake_run)

    cron.install_cron()

    command = seen["command"]
    assert command[command.index("--script") + 1] == ""
    assert command[command.index("--workdir") + 1] == ""
    assert command[-1] == "--agent"


def test_cron_installer_passes_script_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    script = tmp_path / "scripts" / "conscious_agency_gate.py"
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="Created job: job-123\n", stderr="")

    monkeypatch.setattr(cron, "install_gate_script", lambda: script)
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(cron.subprocess, "run", fake_run)
    result = cron.install_cron()
    position = seen["command"].index("--script")
    assert seen["command"][position + 1] == "conscious_agency_gate.py"
    assert result["job_id"] == "job-123"


def test_cron_installer_detects_hermes_soft_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        cron,
        "install_gate_script",
        lambda: tmp_path / "scripts" / "conscious_agency_gate.py",
    )
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(
        cron.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="Failed to create job: rejected", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="rejected"):
        cron.install_cron()


def test_cron_refresh_does_not_duplicate_on_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    AgencyStore(load_config()).set_meta("cron_job_id", "existing-job")
    monkeypatch.setattr(
        cron,
        "install_gate_script",
        lambda: tmp_path / "scripts" / "conscious_agency_gate.py",
    )
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(
        cron.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="Failed to update job: database busy"
        ),
    )
    with pytest.raises(RuntimeError, match="database busy"):
        cron.install_cron()
    assert cron.cron_job_id() == "existing-job"


def test_manual_run_uses_agent_sized_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    AgencyStore(load_config()).set_meta("cron_job_id", "job-123")
    seen = {}

    def fake_run(command, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="Triggered", stderr="")

    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(cron.subprocess, "run", fake_run)
    assert cron.cron_action("run") == "Triggered"
    assert seen["timeout"] == 660


def test_cron_action_detects_soft_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    AgencyStore(load_config()).set_meta("cron_job_id", "missing-job")
    monkeypatch.setattr(cron, "_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(
        cron.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="Job missing-job not found", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="not found"):
        cron.cron_action("run")
