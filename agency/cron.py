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

from .config import hermes_home, load_config
from .engine import AgencyEngine
from .store import AgencyStore

CRON_PROMPT = """\
You are running the Hermes Conscious Agency bounded proactive cycle.

Hard rules:
1. Before doing anything else, call conscious_agency with action="tick".
2. Never call any other tool. This cycle is conversation-only.
3. If reflection_eligible, review focus, intentions, questions, meaningful recent events,
   prior reflections, and recent decisions. Add at most one reflection, only for a new useful
   insight. For add_reflection you must pass summary; insight and confidence are optional.
4. Make at most three other state changes, and only when materially justified.
5. Speak only when speak_eligible is true and one concise message has clear value now.
   Otherwise record decision="silent" with a concrete reason.
6. Never claim sentience, feelings, needs, or private access. Never pressure for a reply.
7. Never perform, schedule, or imply an external action. Send only a check-in or proposal.
8. Before final output, call record_decision with decision and a concrete reason. For speak,
   also pass the exact final message and relevant intention id.
9. Return exactly delivery_text: [SILENT] or the approved message. Add nothing else.
"""


def gate_payload() -> dict[str, Any]:
    config = load_config()
    engine = AgencyEngine(AgencyStore(config), config)
    gates = engine.evaluate_tick()
    return {
        "conscious_agency_gate": gates,
        "instruction": (
            "Reflection eligibility passed. The agent must still call "
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
    if not payload["conscious_agency_gate"].get("reflection_eligible"):
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
                CRON_PROMPT,
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
            CRON_PROMPT,
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
