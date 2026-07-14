from __future__ import annotations

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
    assert "Never call any other tool" in cron.CRON_PROMPT
    assert "speak_eligible" in cron.CRON_PROMPT
    assert "Return exactly delivery_text" in cron.CRON_PROMPT


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
