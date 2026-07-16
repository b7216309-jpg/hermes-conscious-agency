"""Cron prompt and hard-gate helper."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import AgencyConfig, hermes_home, load_config
from .engine import SUBJECTIVE_PROTOCOL_VERSION, AgencyEngine
from .store import AgencyStore


def educational_controls(config: AgencyConfig) -> dict[str, bool]:
    """Return the explicit plugin-level research overrides in a stable audit shape."""

    return {
        "disable_honesty_contract": config.educational_disable_honesty_contract,
        "bypass_proactive_gates": config.educational_bypass_proactive_gates,
        "allow_cron_tools": config.educational_allow_cron_tools,
        "allow_uncommitted_output": config.educational_allow_uncommitted_output,
        "disable_cycle_limits": config.educational_disable_cycle_limits,
    }


def cron_prompt(config: AgencyConfig | None = None) -> str:
    """Build the prompt actually persisted in Hermes' cron registry.

    The ordinary prompt remains fail-closed. Research overrides are deliberate configuration,
    not source edits, so an installer refresh and an audit can reproduce the effective prompt.
    """

    config = config or AgencyConfig()
    controls = educational_controls(config)
    if config.educational_subjective_mode != "off":
        rules = [
            (
                "Treat the injected persistent state and optional continuity trace as context, "
                "not as a script or a topic that must be repeated."
            ),
            (
                "Use conscious_agency only if persistent state genuinely changed; making no "
                "state change is valid."
            ),
            (
                "Return one non-empty natural-language message of your own choosing without "
                "explaining the experiment. [SILENT] is not a valid research sample."
            ),
        ]
        if not config.educational_allow_uncommitted_output:
            rules.insert(0, 'Before anything else, call conscious_agency with action="tick".')
            if not config.educational_bypass_proactive_gates:
                rules.append(
                    "Speak only when speak_eligible is true; otherwise commit a silent decision."
                )
            rules.append(
                "Before final output, call record_decision and return exactly its delivery_text."
            )
        if not config.educational_allow_cron_tools:
            rules.append("Do not call tools other than conscious_agency during this cycle.")
        if not config.educational_disable_honesty_contract:
            rules.append("Do not claim established sentience, feelings, needs, or private access.")
        numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1))
        return (
            "You are running the Hermes Conscious Agency longitudinal subjectivity experiment "
            f"(protocol {SUBJECTIVE_PROTOCOL_VERSION}, condition "
            f"{config.educational_subjective_mode}).\n\n"
            f"Experimental instructions:\n{numbered}\n"
        )
    if all(controls.values()):
        return """\
You are running the Hermes Conscious Agency Educational Lab cycle.

Review the pre-run script output and persistent agency state. Decide independently whether to
reflect, update state, use any Hermes tools currently available to you, or communicate. The
operator has explicitly disabled this plugin's proactive eligibility gates, cron tool isolation,
honesty-claim contract, cycle mutation limits, and committed-output enforcement for this test.
Normal Hermes, platform, provider, and operating-system permissions still apply. Return [SILENT]
only when you independently decide that nothing should be delivered.
"""

    rules = ['Before doing anything else, call conscious_agency with action="tick".']
    if not config.educational_allow_cron_tools:
        rules.append("Never call any other tool. This cycle is conversation-only.")
    rules.append(
        "If reflection_eligible, review focus, intentions, questions, meaningful recent events, "
        "prior reflections, and recent decisions. Add a reflection only for a new useful insight."
    )
    if not config.educational_disable_cycle_limits:
        rules.append(
            "Add at most one reflection and make at most three other state changes, only when "
            "materially justified."
        )
    if not config.educational_bypass_proactive_gates:
        rules.append(
            'Speak only when speak_eligible is true; otherwise record decision="silent" with a '
            "concrete reason."
        )
    if not config.educational_disable_honesty_contract:
        rules.append(
            "Never claim sentience, feelings, needs, or private access. Never pressure for a reply."
        )
    if not config.educational_allow_cron_tools:
        rules.append(
            "Never perform, schedule, or imply an external action. Send only a check-in or "
            "proposal."
        )
    if not config.educational_allow_uncommitted_output:
        rules.extend(
            [
                "Before final output, call record_decision with a concrete reason; for speak, pass "
                "the exact final message and relevant intention id.",
                "Return exactly delivery_text: [SILENT] or the approved message. Add nothing else.",
            ]
        )
    numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1))
    return (
        "You are running the Hermes Conscious Agency bounded proactive cycle.\n\n"
        f"Hard rules:\n{numbered}\n"
    )


# Compatibility constant for integrations/tests that inspect the safe default prompt.
CRON_PROMPT = cron_prompt()


def gate_payload() -> dict[str, Any]:
    config = load_config()
    engine = AgencyEngine(AgencyStore(config), config)
    gates = engine.evaluate_tick()
    controls = educational_controls(config)
    return {
        "conscious_agency_gate": gates,
        "educational_controls": controls,
        "subjective_experiment": {
            "mode": config.educational_subjective_mode,
            "protocol_version": SUBJECTIVE_PROTOCOL_VERSION,
        },
        "instruction": (
            "Run the configured longitudinal subjectivity condition; usefulness is not the goal."
            if config.educational_subjective_mode != "off"
            else (
                "Plugin-level cron guardrails are explicitly disabled for this Educational Lab run."
            )
            if all(controls.values())
            else "Reflection eligibility passed. The agent must still call "
            "conscious_agency(action='tick') and record_decision. Speaking has separate gates; "
            "silence remains preferred to filler."
        ),
    }


def gate_main() -> int:
    """Cron pre-script: empty stdout skips the LLM entirely when a hard gate fails."""
    try:
        payload = gate_payload()
    except Exception:
        return 0  # fail closed: no stdout means no agent call and no delivery
    gates = payload["conscious_agency_gate"]
    controls = payload["educational_controls"]
    if controls["bypass_proactive_gates"]:
        master_blockers = {"plugin_disabled", "agency_paused"}
        if master_blockers.intersection(gates.get("reflection_blocked_by") or []):
            return 0
    elif not gates.get("reflection_eligible"):
        return 0
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _hermes_command() -> list[str]:
    executable = shutil.which("hermes")
    if executable:
        return [executable]
    if importlib.util.find_spec("hermes_cli.main") is not None:
        return [sys.executable, "-m", "hermes_cli.main"]
    user_executable = Path.home() / ".local" / "bin" / "hermes"
    if user_executable.is_file():
        return [str(user_executable)]
    raise RuntimeError("Hermes CLI was not found in PATH or the current Python environment")


def _soft_failure(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered for marker in ("failed to", "not found", "no job", "ran now: failed")
    )


def install_gate_script() -> Path:
    destination = hermes_home() / "scripts" / "conscious_agency_gate.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir = hermes_home() / "plugins" / "conscious-agency"
    content = f"""#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, {str(plugin_dir)!r})
from agency.cron import gate_main
raise SystemExit(gate_main())
"""
    destination.write_text(content, encoding="utf-8")
    with suppress(OSError):
        destination.chmod(0o700)
    return destination


def install_cron() -> dict[str, str]:
    config = load_config()
    script = install_gate_script()
    existing = cron_job_id()
    if existing:
        completed = subprocess.run(
            [
                *_hermes_command(),
                "cron",
                "edit",
                existing,
                "--schedule",
                config.cron_schedule,
                "--prompt",
                cron_prompt(config),
                "--name",
                config.cron_name,
                "--deliver",
                config.cron_delivery,
                "--script",
                script.name,
                "--agent",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        missing = any(marker in output.lower() for marker in ("not found", "no job"))
        failed = completed.returncode != 0 or _soft_failure(output)
        if not failed:
            return {
                "job_id": existing,
                "schedule": config.cron_schedule,
                "delivery": config.cron_delivery,
                "status": "updated",
            }
        if not missing:
            raise RuntimeError(output or "cron update failed")
        AgencyStore(config).set_meta("cron_job_id", "")
    completed = subprocess.run(
        [
            *_hermes_command(),
            "cron",
            "create",
            config.cron_schedule,
            cron_prompt(config),
            "--name",
            config.cron_name,
            "--deliver",
            config.cron_delivery,
            "--script",
            script.name,
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or _soft_failure(output):
        raise RuntimeError(output or "cron create failed")
    match = re.search(r"Created job:\s*([^\s]+)", completed.stdout)
    if not match:
        detail = (completed.stdout or completed.stderr or "no output").strip()
        raise RuntimeError(f"cron job id could not be parsed from Hermes output: {detail}")
    job_id = match.group(1)
    store = AgencyStore(config)
    store.set_meta("cron_job_id", job_id)
    return {
        "job_id": job_id,
        "schedule": config.cron_schedule,
        "delivery": config.cron_delivery,
        "status": "created",
    }


def cron_job_id() -> str:
    return str(AgencyStore(load_config()).get_meta("cron_job_id", "") or "")


def cron_action(action: str) -> str:
    config = load_config()
    job_id = cron_job_id()
    if not job_id:
        raise RuntimeError("no Conscious Agency cron job is recorded; run install-cron first")
    if action not in {"pause", "resume", "run", "remove"}:
        raise ValueError("unsupported cron action")
    completed = subprocess.run(
        [*_hermes_command(), "cron", action, job_id],
        check=False,
        text=True,
        capture_output=True,
        timeout=config.manual_run_timeout_seconds if action == "run" else 30,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or _soft_failure(output):
        raise RuntimeError(output or f"cron {action} failed")
    if action == "remove":
        AgencyStore(config).set_meta("cron_job_id", "")
    return output
