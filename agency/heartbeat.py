"""Gateway-native heartbeat scheduling and delivery.

The scheduling, cooldown, acknowledgement, and HEARTBEAT.md semantics are adapted from
OpenClaw's MIT-licensed heartbeat runtime. Hermes integration is intentionally local to this
module: the heartbeat enters the existing gateway conversation as an internal turn, never as a
Hermes cron session.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .config import AgencyConfig, _parse_clock, _parse_duration, hermes_home, load_config
from .store import AgencyStore

logger = logging.getLogger(__name__)

HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_TRANSCRIPT_PROMPT = "[Hermes assistant-initiated heartbeat; no user message was sent.]"
HEARTBEAT_TRANSCRIPT_NOTE = "Assistant-initiated heartbeat. No user message triggered this turn."
HEARTBEAT_PROMPT = (
    "Read HEARTBEAT.md if it exists in the Hermes home. Follow it strictly. "
    "Do not infer or repeat old tasks from prior chats. Use heartbeat_respond with notify=false "
    "when nothing needs the user's attention, or notify=true with notification_text when the "
    "user should be interrupted."
)
_WAKE_FILE = "heartbeat-wake.json"
_WAKE_LOCK_FILE = "heartbeat-wake.lock"
_RUNNER_LOCK_FILE = "heartbeat-runner.lock"
_STATE_KEY = "heartbeat_state"
_MAX_ACTIVE_SEEK = timedelta(days=7)
_WAKE_THREAD_LOCK = threading.Lock()
_WAKE_PRIORITY = {"scheduled": 0, "event": 1, "immediate": 2, "manual": 3}

WakeIntent = Literal["scheduled", "event", "immediate", "manual"]


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def unrestricted_subjective_heartbeat(config: AgencyConfig) -> bool:
    return config.educational_subjective_mode != "off" and all(
        (
            config.educational_disable_honesty_contract,
            config.educational_bypass_proactive_gates,
            config.educational_allow_uncommitted_output,
            config.educational_disable_cycle_limits,
        )
    )


@dataclass(slots=True)
class HeartbeatTask:
    name: str
    interval: str
    prompt: str


@dataclass(slots=True)
class HeartbeatTurn:
    run_id: str
    prompt: str
    target_session_id: str = ""
    target_source_key: tuple[str, ...] | None = None
    target_route_key: str = ""
    session_key: str = ""
    response: dict[str, Any] | None = None
    raw_output: str = ""
    visible_output: str = ""
    transformed: bool = False
    interrupted_by_user: bool = False
    delivery_started: bool = False
    transcript_committed: bool = False
    delivery_status: str = "pending"
    delivery_error: str = ""
    accepting_user_events: bool = True
    deferred_user_events: list[Any] = dataclasses.field(default_factory=list, repr=False)
    baseline_transcript: list[dict[str, Any]] = dataclasses.field(default_factory=list, repr=False)
    baseline_captured: bool = False
    delivery_enabled: bool = True
    state: dict[str, Any] | None = dataclasses.field(default=None, repr=False)
    decision_id: str = ""
    decision_finalized: bool = False
    response_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)


_heartbeat_turn: contextvars.ContextVar[HeartbeatTurn | None] = contextvars.ContextVar(
    "conscious_agency_heartbeat_turn", default=None
)
_active_heartbeat_lock = threading.Lock()
_active_heartbeats: dict[tuple[str, ...], HeartbeatTurn] = {}


def _source_key(source: Any) -> tuple[str, ...] | None:
    if source is None:
        return None
    platform = getattr(source, "platform", "")
    platform = getattr(platform, "value", platform)
    key = (
        str(platform or ""),
        str(getattr(source, "profile", "") or ""),
        str(getattr(source, "chat_id", "") or ""),
        str(getattr(source, "thread_id", "") or ""),
        str(getattr(source, "user_id", "") or ""),
    )
    return key if any(key) else None


def _register_active_heartbeat(turn: HeartbeatTurn, source: Any, route_key: str = "") -> None:
    key = _source_key(source)
    turn.target_source_key = key
    turn.target_route_key = str(route_key or "")
    if key is not None:
        with _active_heartbeat_lock:
            _active_heartbeats[key] = turn


def _unregister_active_heartbeat(turn: HeartbeatTurn) -> None:
    key = turn.target_source_key
    if key is None:
        return
    with _active_heartbeat_lock:
        if _active_heartbeats.get(key) is turn:
            _active_heartbeats.pop(key, None)


def current_heartbeat_turn() -> HeartbeatTurn | None:
    return _heartbeat_turn.get()


def release_heartbeat_for_user_turn(event: Any, gateway: Any = None) -> HeartbeatTurn | None:
    """Preempt a matching heartbeat for an authenticated real-user turn.

    Hermes invokes ``pre_gateway_dispatch`` before its own authorization gate.  Re-checking
    authorization here prevents an unknown sender from cancelling a legitimate heartbeat.
    The real event is retained and replayed through Hermes after the heartbeat releases the
    durable chat lane. This prevents the busy-session interrupt recursion from executing a real
    user turn inside the heartbeat's ContextVar scope or losing it during final delivery.
    """

    if event is None or bool(getattr(event, "internal", False)):
        return None
    if gateway is not None:
        authorize = getattr(gateway, "_is_user_authorized", None)
        if not callable(authorize):
            return None
        try:
            if not bool(authorize(getattr(event, "source", None))):
                return None
        except Exception:
            logger.warning("Heartbeat preemption authorization check failed", exc_info=True)
            return None

    current = current_heartbeat_turn()
    key = _source_key(getattr(event, "source", None))
    route_key = ""
    if gateway is not None:
        route = getattr(gateway, "_session_key_for_source", None)
        if callable(route):
            with contextlib.suppress(Exception):
                route_key = str(route(getattr(event, "source", None)) or "")
    with _active_heartbeat_lock:
        active = _active_heartbeats.get(key) if key is not None else None
        if active is None and route_key:
            active = next(
                (
                    candidate
                    for candidate in _active_heartbeats.values()
                    if candidate.target_route_key == route_key
                ),
                None,
            )
    turn = active or (
        current
        if current is not None
        and (current.target_source_key is None or current.target_source_key == key)
        else None
    )
    if turn is None:
        return None
    with turn.response_lock:
        if not turn.accepting_user_events:
            return None
        turn.deferred_user_events.append(event)
        # Once transcript commit/delivery begins it is no longer safe to recall
        # the message. The genuine turn still queues and will run immediately
        # after the ordered delivery boundary.
        should_interrupt = not turn.delivery_started
        if should_interrupt:
            turn.interrupted_by_user = True
    if gateway is not None and should_interrupt and turn.session_key:
        running = getattr(gateway, "_running_agents", {}).get(turn.session_key)
        if running is not None and hasattr(running, "interrupt"):
            with contextlib.suppress(Exception):
                running.interrupt("native heartbeat yielded to a real user message")
    return turn


@contextlib.contextmanager
def heartbeat_turn(turn: HeartbeatTurn):
    token = _heartbeat_turn.set(turn)
    try:
        yield turn
    finally:
        _heartbeat_turn.reset(token)


def record_heartbeat_response(notify: bool, notification_text: str = "") -> dict[str, Any]:
    turn = current_heartbeat_turn()
    if turn is None:
        raise PermissionError("heartbeat_respond is available only during a native heartbeat turn")
    if type(notify) is not bool:
        raise TypeError("notify must be a boolean")
    text = str(notification_text or "").strip()
    if notify and not text:
        raise ValueError("notification_text is required when notify=true")
    # Parallel tool batches and confused models can submit the decision more than once.
    # The first valid decision is authoritative; later calls cannot replace it.
    with turn.response_lock:
        if turn.response is None:
            turn.response = {"notify": notify, "notification_text": text}
            accepted = True
        else:
            accepted = False
        return {
            **turn.response,
            "accepted": accepted,
            "instruction": (
                "Heartbeat decision recorded. End the turn now without another tool call."
            ),
        }


def heartbeat_response() -> dict[str, Any] | None:
    turn = current_heartbeat_turn()
    return dict(turn.response) if turn and turn.response is not None else None


def _strip_leading_html_comments(line: str, state: dict[str, bool]) -> str:
    remaining = line
    while state["comment"] or remaining.lstrip().startswith("<!--"):
        search = remaining if state["comment"] else remaining.lstrip()
        end = search.find("-->")
        if end < 0:
            state["comment"] = True
            return ""
        state["comment"] = False
        if search == remaining:
            remaining = remaining[end + 3 :]
        else:
            width = len(remaining) - len(search)
            remaining = remaining[:width] + search[end + 3 :]
    return remaining


def _without_html_comments(content: str) -> list[str]:
    state = {"comment": False}
    return [_strip_leading_html_comments(line, state) for line in content.splitlines()]


def heartbeat_content_effectively_empty(content: str | None) -> bool:
    """Return true for the comment/header-only template shipped by the plugin."""

    if content is None:
        return False
    for line in _without_html_comments(content):
        trimmed = line.strip()
        if not trimmed:
            continue
        if re.fullmatch(r"#+(?:\s.*)?", trimmed):
            continue
        if re.fullmatch(r"[-*+]\s*(?:\[[\sXx]?\]\s*)?", trimmed):
            continue
        if re.fullmatch(r"```[A-Za-z0-9_-]*", trimmed):
            continue
        if re.fullmatch(r"<!--.*-->", trimmed):
            continue
        return False
    return True


def parse_heartbeat_tasks(content: str) -> list[HeartbeatTask]:
    """Parse OpenClaw-compatible YAML-like task entries from HEARTBEAT.md."""

    lines = _without_html_comments(content)
    tasks: list[HeartbeatTask] = []
    in_tasks = False
    in_fence = False
    for index, line in enumerate(lines):
        trimmed = line.strip()
        if trimmed.startswith("```"):
            in_fence = not in_fence
            if in_tasks:
                in_tasks = False
            continue
        if not in_fence and line == "tasks:":
            in_tasks = True
            continue
        if not in_tasks:
            continue
        if trimmed and not line[:1].isspace() and not trimmed.startswith("- name:"):
            in_tasks = False
            continue
        if not trimmed.startswith("- name:"):
            continue
        name = trimmed.removeprefix("- name:").strip().strip("\"'")
        interval = ""
        prompt = ""
        for following in lines[index + 1 :]:
            next_trimmed = following.strip()
            if next_trimmed.startswith("- name:"):
                break
            if next_trimmed and not following[:1].isspace():
                break
            if next_trimmed.startswith("interval:"):
                interval = next_trimmed.removeprefix("interval:").strip().strip("\"'")
            elif next_trimmed.startswith("prompt:"):
                prompt = next_trimmed.removeprefix("prompt:").strip().strip("\"'")
        if not (name and interval and prompt):
            continue
        try:
            _parse_duration(interval)
        except ValueError:
            continue
        tasks.append(HeartbeatTask(name=name[:120], interval=interval, prompt=prompt[:4000]))
    return tasks


def _content_without_tasks(content: str) -> str:
    lines = content.splitlines()
    kept: list[str] = []
    in_tasks = False
    in_fence = False
    for line in lines:
        trimmed = line.strip()
        if trimmed.startswith("```"):
            in_fence = not in_fence
            if in_tasks:
                in_tasks = False
            kept.append(line)
            continue
        if not in_fence and line == "tasks:":
            in_tasks = True
            continue
        if in_tasks:
            if trimmed and not line[:1].isspace() and not trimmed.startswith("- name:"):
                in_tasks = False
            else:
                continue
        if not in_tasks:
            kept.append(line)
    return "\n".join(kept).strip()


def is_task_due(last_run: float | None, interval: str, now: float) -> bool:
    return last_run is None or now - float(last_run) >= _parse_duration(interval)


def heartbeat_phase_seconds(seed: str, agent_id: str, interval_seconds: float) -> float:
    interval_ms = max(1, int(interval_seconds * 1000))
    digest = hashlib.sha256(f"{seed}:{agent_id}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") % interval_ms) / 1000.0


def next_phase_due(now: float, interval_seconds: float, phase_seconds: float) -> float:
    interval = max(1.0, float(interval_seconds))
    phase = phase_seconds % interval
    position = now % interval
    delta = (phase - position) % interval
    if delta == 0:
        delta = interval
    return now + delta


def _active_at(epoch: float, config: AgencyConfig) -> bool:
    if not config.heartbeat_active_hours_start:
        return True
    local = datetime.fromtimestamp(epoch, UTC).astimezone(ZoneInfo(config.timezone)).time()
    start_h, start_m = _parse_clock(config.heartbeat_active_hours_start)
    if config.heartbeat_active_hours_end == "24:00":
        end_h, end_m = 24, 0
    else:
        end_h, end_m = _parse_clock(config.heartbeat_active_hours_end)
    current = local.hour * 60 + local.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def seek_active_due(start: float, interval_seconds: float, config: AgencyConfig) -> float:
    if not config.heartbeat_active_hours_start:
        return start
    interval = max(float(interval_seconds), 1.0)
    horizon = start + _MAX_ACTIVE_SEEK.total_seconds()

    # Preserve the deterministic phase whenever an aligned slot exists in the
    # next week. For sub-30-second cadences, phase-aligned batches bound the
    # scan without missing a minute-granular active-hours transition.
    multiplier = max(1, math.ceil(30.0 / interval))
    phase_step = interval * multiplier
    candidate = start
    previous_inactive: float | None = None
    while candidate < horizon:
        if _active_at(candidate, config):
            if previous_inactive is not None and multiplier > 1:
                inactive = previous_inactive
                active = candidate
                while active - inactive > interval:
                    remaining = (active - inactive) / interval
                    probe = inactive + math.floor(remaining / 2) * interval
                    if _active_at(probe, config):
                        active = probe
                    else:
                        inactive = probe
                return active
            return candidate
        previous_inactive = candidate
        candidate += phase_step

    # A cadence longer than its active window can have no aligned slot at all.
    # Fall back to the earliest active wall-clock instant so the heartbeat is
    # not starved forever merely because its stable phase falls outside the
    # window. This fallback is only reached after phase-aligned seeking fails.
    candidate = start
    while candidate < horizon:
        if _active_at(candidate, config):
            return candidate
        candidate += min(interval, 60.0)

    # A closed active window intentionally has no valid instant. Sleeping to
    # the horizon avoids a hot reschedule loop while retaining that policy.
    return horizon


def strip_heartbeat_ack(raw: str, max_ack_chars: int = 300) -> tuple[bool, str]:
    """Return ``(silent, visible_text)`` using OpenClaw's edge-token contract."""

    text = str(raw or "").strip()
    if not text:
        return True, ""
    normalized = re.sub(r"<[^>]*>", " ", text, flags=re.I)
    normalized = re.sub(r"&nbsp;", " ", normalized, flags=re.I).strip("*`~_ \t\r\n")
    candidate = normalized
    if HEARTBEAT_OK not in candidate:
        return False, text
    stripped = candidate.strip()
    changed = False
    while True:
        if stripped.startswith(HEARTBEAT_OK):
            stripped = stripped[len(HEARTBEAT_OK) :].lstrip()
            changed = True
            continue
        match = re.search(re.escape(HEARTBEAT_OK) + r"[^\w]{0,4}$", stripped)
        if match:
            stripped = stripped[: match.start()].rstrip()
            changed = True
            continue
        break
    if not changed:
        return False, text
    visible = re.sub(r"\s+", " ", stripped).strip()
    if not visible or len(visible) <= max_ack_chars:
        return True, ""
    return False, visible


def should_defer_wake(
    *,
    intent: WakeIntent,
    now: float,
    next_due: float,
    last_started: float | None,
    recent_starts: list[float],
    min_spacing: float = 30.0,
    flood_window: float = 60.0,
    flood_threshold: int = 5,
) -> str:
    if intent == "manual":
        return ""
    recent = [stamp for stamp in recent_starts if stamp >= now - flood_window]
    if len(recent) >= flood_threshold:
        return "flood"
    if intent == "immediate":
        return ""
    if intent == "scheduled":
        return "not_due" if now < next_due else ""
    if last_started is None:
        return ""
    if now < next_due:
        return "not_due"
    if min_spacing > 0 and now - last_started < min_spacing:
        return "min_spacing"
    return ""


def _scheduler_seed() -> str:
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        with contextlib.suppress(OSError):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return socket.gethostname() or "hermes"


def _wake_path() -> Path:
    return hermes_home() / "conscious-agency" / _WAKE_FILE


def _normalized_wake(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    request_id = str(value.get("request_id") or "").strip()
    intent = str(value.get("intent") or "").strip()
    requested_at = _finite_float(value.get("requested_at"), -1.0)
    if not request_id or intent not in _WAKE_PRIORITY or requested_at < 0:
        return None
    return {
        "request_id": request_id[:128],
        "intent": intent,
        "reason": str(value.get("reason") or "")[:300],
        "requested_at": requested_at,
    }


@contextlib.contextmanager
def _wake_file_lock():
    """Serialize wake read/replace/unlink across threads and local processes."""

    path = _wake_path().with_name(_WAKE_LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WAKE_THREAD_LOCK, path.open("a+b") as handle:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    with contextlib.suppress(OSError):
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def request_heartbeat_wake(intent: WakeIntent = "manual", reason: str = "operator") -> str:
    if intent not in _WAKE_PRIORITY:
        raise ValueError("heartbeat wake intent must be scheduled, event, immediate, or manual")
    request_id = uuid.uuid4().hex
    path = _wake_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": request_id,
        "intent": intent,
        "reason": str(reason or "")[:300],
        "requested_at": time.time(),
    }
    with _wake_file_lock():
        existing: dict[str, Any] | None = None
        with contextlib.suppress(OSError, ValueError):
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                existing = candidate
        if existing is not None:
            old_intent = str(existing.get("intent") or "event")
            if _WAKE_PRIORITY.get(old_intent, 1) > _WAKE_PRIORITY[intent]:
                return str(existing.get("request_id") or request_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                temporary.chmod(0o600)
            os.replace(temporary, path)
            with contextlib.suppress(OSError):
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return request_id


def _migrate_legacy_config() -> dict[str, Any]:
    path = hermes_home() / "config.yaml"
    if not path.is_file():
        return {"changed": False, "backup": None}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to migrate Agency configuration") from exc
    document = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(document, dict):
        raise ValueError("Hermes configuration root must be a mapping")
    plugins = document.get("plugins")
    if not isinstance(plugins, dict):
        return {"changed": False, "backup": None}
    section = plugins.get("conscious-agency") or plugins.get("conscious_agency")
    if not isinstance(section, dict):
        entries = plugins.get("entries")
        entry = (
            entries.get("conscious_agency") or entries.get("conscious-agency")
            if isinstance(entries, dict)
            else None
        )
        section = entry.get("config", entry) if isinstance(entry, dict) else None
    if not isinstance(section, dict):
        return {"changed": False, "backup": None}

    before = dict(section)
    if "heartbeat_enabled" not in section and "allow_scheduled_reflection" in section:
        section["heartbeat_enabled"] = section["allow_scheduled_reflection"]
    if "heartbeat_every" not in section and "cron_schedule" in section:
        schedule = str(section.get("cron_schedule") or "").strip().lower()
        section["heartbeat_every"] = schedule.removeprefix("every ") or "30m"
    if "heartbeat_target" not in section and "cron_delivery" in section:
        section["heartbeat_target"] = "none" if section.get("cron_delivery") == "local" else "last"
    if "heartbeat_disable_thinking" not in section and "cron_disable_thinking" in section:
        section["heartbeat_disable_thinking"] = section["cron_disable_thinking"]
    if (
        "educational_allow_heartbeat_tools" not in section
        and "educational_allow_cron_tools" in section
    ):
        section["educational_allow_heartbeat_tools"] = section["educational_allow_cron_tools"]
    for retired in (
        "allow_scheduled_reflection",
        "cron_schedule",
        "cron_delivery",
        "cron_name",
        "cron_disable_thinking",
        "manual_run_timeout_seconds",
        "educational_allow_cron_tools",
        "heartbeat_max_iterations",
    ):
        section.pop(retired, None)
    if section == before:
        return {"changed": False, "backup": None}

    backup_dir = hermes_home() / "conscious-agency" / "migrations"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"config-before-heartbeat-{stamp}.yaml"
    shutil.copy2(path, backup)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    for protected in (backup_dir, backup, path):
        with contextlib.suppress(OSError):
            protected.chmod(0o700 if protected == backup_dir else 0o600)
    return {"changed": True, "backup": str(backup)}


def remove_legacy_cron() -> dict[str, Any]:
    """Remove only the job previously recorded by Conscious Agency 0.6.x."""

    config = load_config()
    store = AgencyStore(config)
    config_migration = _migrate_legacy_config()
    job_id = str(store.get_meta("cron_job_id", "") or "")
    removed = False
    output = ""
    if job_id:
        executable = shutil.which("hermes")
        if not executable:
            raise RuntimeError("cannot remove the recorded Agency cron: hermes is not on PATH")
        completed = subprocess.run(
            [executable, "cron", "remove", job_id],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        missing = any(marker in output.casefold() for marker in ("not found", "no job"))
        if completed.returncode != 0 and not missing:
            raise RuntimeError(output or "legacy Agency cron removal failed")
        removed = completed.returncode == 0 or missing
        store.set_meta("cron_job_id", "")
    gate = hermes_home() / "scripts" / "conscious_agency_gate.py"
    with contextlib.suppress(OSError):
        gate.unlink()
    state = store.get_meta(_STATE_KEY, {})
    state = dict(state) if isinstance(state, dict) else {}
    state["legacy_cron_removed"] = True
    state["legacy_cron_removed_at"] = time.time()
    store.set_meta(_STATE_KEY, state)
    return {
        "job_id": job_id,
        "removed": removed,
        "output": output,
        "config_migration": config_migration,
    }


def _peek_wake() -> dict[str, Any] | None:
    """Read a wake without consuming it.

    The runner first persists the returned request in the encrypted Agency
    store, then acknowledges the file.  Keeping those as two ordered steps
    prevents a process crash from losing the only durable copy of a wake.
    """

    path = _wake_path()
    with _wake_file_lock():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return _normalized_wake(payload)


def _ack_wake(request_id: str) -> bool:
    """Remove exactly the wake that was durably accepted by the runner."""

    if not request_id:
        return False
    path = _wake_path()
    with _wake_file_lock():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            return False
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            return False
        path.unlink(missing_ok=True)
        return True


def heartbeat_status(store: AgencyStore | None = None) -> dict[str, Any]:
    config = load_config()
    value = (store or AgencyStore(config)).get_meta(_STATE_KEY, {})
    state = value if isinstance(value, dict) else {}
    pending = _normalized_wake(state.get("pending_wake")) or {}
    claimed = _normalized_wake(state.get("claimed_wake")) or {}
    requested_at = _finite_float(pending.get("requested_at"))
    claimed_at = _finite_float(claimed.get("requested_at"))
    inflight = state.get("inflight")
    inflight = inflight if isinstance(inflight, dict) else {}
    delivery = state.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    last_started = _finite_float(state.get("last_started_at"))
    last_completed = _finite_float(state.get("last_completed_at"))
    return {
        "enabled": config.heartbeat_enabled,
        "every": config.heartbeat_every,
        "target": config.heartbeat_target,
        "active_hours": (
            {
                "start": config.heartbeat_active_hours_start,
                "end": config.heartbeat_active_hours_end,
                "timezone": config.timezone,
            }
            if config.heartbeat_active_hours_start
            else None
        ),
        "next_due_at": _finite_float(state.get("next_due_at")) or None,
        "last_started_at": last_started or None,
        "last_completed_at": last_completed or None,
        "last_run_id": str(state.get("last_run_id") or ""),
        "last_status": str(state.get("last_status") or "never_started")[:80],
        "last_reason": str(state.get("last_reason") or "")[:500],
        "runs": _nonnegative_int(state.get("runs")),
        "attempts": _nonnegative_int(state.get("attempts")),
        "consecutive_failures": _nonnegative_int(state.get("consecutive_failures")),
        "run_in_progress": bool(inflight)
        or (state.get("last_status") == "running" and last_started > last_completed),
        "pending_wake": {
            "present": bool(pending),
            "intent": str(pending.get("intent") or "") if pending else "",
            "requested_at": requested_at or None,
            "age_seconds": max(0.0, time.time() - requested_at) if requested_at else None,
        },
        "claimed_wake": {
            "present": bool(claimed),
            "intent": str(claimed.get("intent") or "") if claimed else "",
            "requested_at": claimed_at or None,
            "age_seconds": max(0.0, time.time() - claimed_at) if claimed_at else None,
            "owned_by_run": bool(
                claimed
                and inflight
                and str(inflight.get("wake_request_id") or "")
                == str(claimed.get("request_id") or "")
            ),
        },
        "delivery": {
            "run_id": str(delivery.get("run_id") or "") if delivery else "",
            "status": str(delivery.get("status") or "") if delivery else "",
            "started_at": _finite_float(delivery.get("started_at")) or None,
            "finished_at": _finite_float(delivery.get("finished_at")) or None,
        },
        "legacy_cron_removed": bool(state.get("legacy_cron_removed", False)),
        "runner": {
            "active": bool(state.get("runner_active", False)),
            "pid": _nonnegative_int(state.get("runner_pid")),
            "instance_id": str(state.get("runner_instance_id") or ""),
            "started_at": _finite_float(state.get("runner_started_at")) or None,
            "stopped_at": _finite_float(state.get("runner_stopped_at")) or None,
        },
    }


class HeartbeatRunner:
    _agency_heartbeat_runner = True

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store: AgencyStore = runtime.store
        self._gateway_ref: weakref.ReferenceType[Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._run_lock = asyncio.Lock()
        self._instance_id = uuid.uuid4().hex
        self._runner_lock_handle: Any | None = None

    def rebind(self, runtime: Any) -> None:
        """Adopt a hot-reloaded plugin runtime without starting a second loop."""

        self.runtime = runtime
        self.store = runtime.store

    def start(self, gateway: Any) -> asyncio.Task[Any] | None:
        self._gateway_ref = weakref.ref(gateway)
        if self._task and not self._task.done():
            return self._task
        if not self._acquire_runner_lock():
            logger.warning(
                "Conscious Agency heartbeat runner not started: another process owns the lease"
            )
            with contextlib.suppress(Exception):
                self.runtime.store.add_event(
                    "heartbeat_runner_rejected",
                    summary="A duplicate heartbeat runner was prevented",
                    metadata={"pid": os.getpid()},
                )
            return None
        state = self._load_state()
        state.update(
            {
                "runner_active": True,
                "runner_pid": os.getpid(),
                "runner_instance_id": self._instance_id,
                "runner_started_at": time.time(),
            }
        )
        state.pop("runner_stopped_at", None)
        self._save_state(state)
        try:
            self._task = asyncio.create_task(
                self.run(gateway),
                name="conscious-agency-heartbeat",
                context=contextvars.Context(),
            )
        except Exception:
            try:
                self._mark_runner_stopped()
            finally:
                self._release_runner_lock()
            raise
        self._task.add_done_callback(lambda _task: self._runner_finished())
        return self._task

    def stop(self) -> None:
        try:
            self._mark_runner_stopped()
        finally:
            task = self._task
            if task is not None and not task.done():
                task.cancel()
            elif task is None:
                self._release_runner_lock()

    def _runner_lock_path(self) -> Path:
        return hermes_home() / "conscious-agency" / _RUNNER_LOCK_FILE

    def _acquire_runner_lock(self) -> bool:
        if self._runner_lock_handle is not None:
            return True
        path = self._runner_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        self._runner_lock_handle = handle
        return True

    def _release_runner_lock(self) -> None:
        handle = self._runner_lock_handle
        self._runner_lock_handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _runner_finished(self) -> None:
        try:
            self._mark_runner_stopped()
        finally:
            self._release_runner_lock()

    def _mark_runner_stopped(self) -> None:
        state = self._load_state()
        if state.get("runner_instance_id") != self._instance_id:
            return
        state["runner_active"] = False
        state["runner_stopped_at"] = time.time()
        self._save_state(state)

    def _load_state(self) -> dict[str, Any]:
        value = self.store.get_meta(_STATE_KEY, {})
        state = dict(value) if isinstance(value, dict) else {}
        raw_recent = state.get("recent_starts")
        recent = raw_recent if isinstance(raw_recent, list) else []
        state["recent_starts"] = [
            parsed for item in recent[-101:] if (parsed := _finite_float(item, -1.0)) >= 0
        ]
        for key in ("task_last_runs", "commitment_last_runs"):
            value = state.get(key)
            state[key] = dict(value) if isinstance(value, dict) else {}
        for key in ("runs", "attempts", "consecutive_failures"):
            state[key] = _nonnegative_int(state.get(key))
        for key in (
            "next_due_at",
            "last_started_at",
            "last_completed_at",
            "last_interrupted_at",
            "runner_started_at",
            "runner_stopped_at",
        ):
            if key in state:
                state[key] = _finite_float(state.get(key))
        for key in ("pending_wake", "claimed_wake", "delivery", "inflight"):
            if key in state and not isinstance(state.get(key), dict):
                state.pop(key, None)
        for key in ("pending_wake", "claimed_wake"):
            if key in state:
                normalized = _normalized_wake(state.get(key))
                if normalized is None:
                    state.pop(key, None)
                else:
                    state[key] = normalized
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.store.set_meta(_STATE_KEY, state)

    def _set_decision_status(self, decision_id: str, status: str) -> None:
        if not decision_id:
            return
        try:
            self.store.update_decision_delivery(decision_id, status)
        except Exception:
            # Delivery must not be duplicated merely because optional ledger
            # bookkeeping failed.  The heartbeat event/state path still
            # records the transport outcome for operator reconciliation.
            logger.warning("Heartbeat decision delivery status could not be updated", exc_info=True)

    def _set_turn_decision_status(self, turn: HeartbeatTurn, status: str) -> None:
        if not turn.decision_finalized:
            self._set_decision_status(turn.decision_id, status)

    @staticmethod
    def _consume_claimed_wake(state: dict[str, Any], request_id: str) -> None:
        claim = _normalized_wake(state.get("claimed_wake"))
        if claim and claim["request_id"] == request_id:
            state.pop("claimed_wake", None)

    @staticmethod
    def _restore_unstarted_wake(state: dict[str, Any]) -> bool:
        """Return a claimed wake to the queue when no model run owns it."""

        claim = _normalized_wake(state.get("claimed_wake"))
        if claim is None or bool(state.get("inflight")):
            return False
        pending = _normalized_wake(state.get("pending_wake"))
        if pending is None or _WAKE_PRIORITY[claim["intent"]] >= _WAKE_PRIORITY[pending["intent"]]:
            state["pending_wake"] = claim
        state.pop("claimed_wake", None)
        return True

    def _reconcile_interrupted_run(self) -> None:
        """Close an unfinished run left by a gateway stop or process crash."""

        state = self._load_state()
        started = _finite_float(state.get("last_started_at"))
        completed = _finite_float(state.get("last_completed_at"))
        if started <= completed:
            return
        if (
            state.get("last_status") == "interrupted"
            and _finite_float(state.get("last_interrupted_at")) >= started
        ):
            return
        inflight = state.get("inflight")
        inflight = inflight if isinstance(inflight, dict) else {}
        run_id = str(inflight.get("run_id") or "")
        delivery = state.get("delivery")
        delivery = delivery if isinstance(delivery, dict) else {}
        current_delivery = bool(run_id and delivery.get("run_id") == run_id)
        delivery_status = str(delivery.get("status") or "") if current_delivery else ""
        decision_id = str(inflight.get("decision_id") or delivery.get("decision_id") or "")
        if decision_id:
            if delivery_status == "delivered":
                self._set_decision_status(decision_id, "delivered")
            elif delivery_status in {"sending", "ambiguous"}:
                self._set_decision_status(decision_id, "ambiguous")
            else:
                self._set_decision_status(decision_id, "interrupted")
        completed = time.time()
        self._close_inflight(
            state,
            completed,
            consume_due=delivery_status in {"sending", "ambiguous", "delivered"},
        )
        state["last_status"] = "interrupted"
        state["last_reason"] = "gateway_restart"
        state["last_interrupted_at"] = time.time()
        event_kind = "heartbeat_interrupted"
        summary = "An unfinished heartbeat was closed after gateway restart"
        if current_delivery and delivery.get("status") == "delivered":
            state["last_status"] = "delivered"
            state["last_reason"] = "gateway_restart_after_delivery"
            state.pop("last_interrupted_at", None)
            event_kind = "heartbeat_delivery_reconciled"
            summary = "A delivered heartbeat was finalized after gateway restart"
        elif current_delivery and delivery_status in {"sending", "ambiguous"}:
            delivery = dict(delivery)
            delivery["status"] = "ambiguous"
            delivery["reconciled_at"] = time.time()
            state["delivery"] = delivery
            state["last_reason"] = "gateway_restart_during_delivery"
        self._save_state(state)
        self.runtime.store.add_event(
            event_kind,
            summary=summary,
            metadata={
                "run_id": run_id,
                "delivery_status": str(delivery.get("status") or ""),
            },
        )

    def _close_inflight(
        self, state: dict[str, Any], completed_at: float, *, consume_due: bool
    ) -> None:
        inflight = state.get("inflight")
        inflight = inflight if isinstance(inflight, dict) else {}
        if consume_due:
            task_state = dict(state.get("task_last_runs") or {})
            for name in inflight.get("due_tasks") or []:
                if isinstance(name, str) and name:
                    task_state[name] = completed_at
            state["task_last_runs"] = task_state
            commitment_state = dict(state.get("commitment_last_runs") or {})
            for item_id in inflight.get("due_commitments") or []:
                clean_id = str(item_id or "")
                if not clean_id:
                    continue
                try:
                    item = self.store.get_intention(clean_id) or {}
                except Exception:
                    # Delivery/task completion must remain final even if optional
                    # revision lookup is temporarily unavailable. A blank revision
                    # makes a later edit eligible again instead of losing it.
                    item = {}
                commitment_state[clean_id] = {
                    "ran_at": completed_at,
                    "revision": str(item.get("updated_at") or ""),
                }
            state["commitment_last_runs"] = commitment_state
        state.pop("inflight", None)
        state["last_completed_at"] = completed_at

    def _record_status(
        self, state: dict[str, Any], status: str, reason: str = "", *, force: bool = False
    ) -> None:
        clean_reason = str(reason or "")[:500]
        changed = state.get("last_status") != status or state.get("last_reason") != clean_reason
        state["last_status"] = status
        state["last_reason"] = clean_reason
        if changed or force:
            self._save_state(state)

    def _next_scheduled(self, now: float, config: AgencyConfig) -> float:
        interval = _parse_duration(config.heartbeat_every)
        phase = heartbeat_phase_seconds(_scheduler_seed(), "conscious-agency", interval)
        return seek_active_due(next_phase_due(now, interval, phase), interval, config)

    def _next_scheduled_after_wake(self, completed_at: float, config: AgencyConfig) -> float:
        """Let a completed external wake satisfy an imminently due phase.

        Manual, event, and immediate wakes are real heartbeat opportunities.
        Without this coalescing, a wake just before the deterministic phase can
        produce a second proactive message seconds later and look like a loop.
        Keep the phase stable, but skip one edge when it is less than half an
        interval away.
        """

        interval = _parse_duration(config.heartbeat_every)
        candidate = self._next_scheduled(completed_at, config)
        if candidate - completed_at < interval / 2:
            return self._next_scheduled(candidate + 0.001, config)
        return candidate

    @staticmethod
    def _gateway_busy(gateway: Any) -> bool:
        if getattr(gateway, "_draining", False) or getattr(
            gateway, "_startup_restore_in_progress", False
        ):
            return True
        try:
            return int(gateway._active_work_count()) > 0
        except Exception:
            return bool(getattr(gateway, "_running_agents", {}))

    @staticmethod
    def _background_busy(gateway: Any) -> bool:
        """Optional extra-busy policy beyond Hermes' core active work."""

        for name in ("_active_background_process_count", "_background_work_count"):
            probe = getattr(gateway, name, None)
            if callable(probe):
                with contextlib.suppress(Exception):
                    if int(probe()) > 0:
                        return True
        return False

    @staticmethod
    def _failure_delay(state: dict[str, Any]) -> float:
        failures = max(1, _nonnegative_int(state.get("consecutive_failures"), 1))
        return min(6 * 3600.0, 30.0 * (2 ** min(failures - 1, 10)))

    def _finalize_exception(self, state: dict[str, Any], exc: Exception) -> bool:
        completed = time.time()
        inflight = state.get("inflight")
        inflight = inflight if isinstance(inflight, dict) else {}
        delivery = state.get("delivery")
        delivery = delivery if isinstance(delivery, dict) else {}
        run_id = str(inflight.get("run_id") or state.get("last_run_id") or "")
        current_delivery = bool(run_id and delivery.get("run_id") == run_id)
        delivery_status = str(delivery.get("status") or "") if current_delivery else ""
        ambiguous = current_delivery and delivery.get("status") == "ambiguous"
        delivered = current_delivery and delivery.get("status") == "delivered"
        session_id = str(inflight.get("target_session_id") or "")
        decision_id = str(inflight.get("decision_id") or delivery.get("decision_id") or "")
        if decision_id:
            if delivered:
                self._set_decision_status(decision_id, "delivered")
            elif ambiguous:
                self._set_decision_status(decision_id, "ambiguous")
            else:
                self._set_decision_status(decision_id, "failed")
        if not inflight and delivery_status in {"delivered", "silent", "suppressed", "interrupted"}:
            reason_prefix = "post_delivery" if delivered else "post_finalization"
            self._record_status(
                state,
                delivery_status,
                f"{reason_prefix}_{type(exc).__name__}",
                force=True,
            )
            with contextlib.suppress(Exception):
                self.runtime.store.add_event(
                    "heartbeat_delivery_reconciled",
                    session_id=str(delivery.get("target_session_id") or ""),
                    summary="A finalized heartbeat survived a bookkeeping failure",
                    metadata={
                        "run_id": run_id,
                        "delivery_status": delivery_status,
                        "error_type": type(exc).__name__,
                    },
                )
            return True
        if inflight:
            self._close_inflight(state, completed, consume_due=ambiguous or delivered)
        else:
            self._restore_unstarted_wake(state)
        if current_delivery and not ambiguous and not delivered:
            delivery = dict(delivery)
            delivery["status"] = "failed"
            delivery["finished_at"] = completed
            state["delivery"] = delivery
        if delivered:
            state["consecutive_failures"] = 0
        else:
            state["consecutive_failures"] = _nonnegative_int(state.get("consecutive_failures")) + 1
        if not ambiguous and not delivered:
            state["next_due_at"] = completed + self._failure_delay(state)
        if delivered:
            self._record_status(
                state,
                "delivered",
                f"post_delivery_{type(exc).__name__}",
                force=True,
            )
            self.runtime.store.add_event(
                "heartbeat_delivery_reconciled",
                session_id=session_id,
                summary="Heartbeat delivery completed before a bookkeeping failure",
                metadata={"run_id": run_id, "error_type": type(exc).__name__},
            )
            return True
        self._record_status(
            state,
            "failed",
            "ambiguous_delivery" if ambiguous else type(exc).__name__,
            force=True,
        )
        self.runtime.store.add_event(
            "heartbeat_failed",
            session_id=session_id,
            summary="Native heartbeat turn failed",
            metadata={
                "run_id": run_id,
                "ambiguous_delivery": ambiguous,
                "error_type": type(exc).__name__,
            },
        )
        return ambiguous

    async def run(self, gateway: Any) -> None:
        logger.info("Conscious Agency native heartbeat runner started")
        try:
            self._reconcile_interrupted_run()
        except Exception:
            # Startup reconciliation is retried naturally by the durable state
            # path.  A transient telemetry/store error must not kill the sole
            # scheduler task for the lifetime of the gateway.
            logger.exception("Conscious Agency heartbeat startup reconciliation failed")
        while bool(getattr(gateway, "_running", False)):
            state: dict[str, Any] | None = None
            try:
                config = load_config()
                self.runtime.reload_config(config)
                state = self._load_state()
                if self._restore_unstarted_wake(state):
                    self._save_state(state)
                now = time.time()
                schedule_signature = "|".join(
                    (
                        config.heartbeat_every,
                        config.heartbeat_active_hours_start,
                        config.heartbeat_active_hours_end,
                        config.timezone,
                    )
                )
                if state.get("schedule_signature") != schedule_signature:
                    state["schedule_signature"] = schedule_signature
                    state["next_due_at"] = self._next_scheduled(now, config)
                    self._save_state(state)
                next_due = _finite_float(state.get("next_due_at"))
                if next_due <= 0:
                    next_due = self._next_scheduled(now, config)
                    state["next_due_at"] = next_due
                    self._save_state(state)
                wake = state.get("pending_wake")
                if isinstance(wake, dict):
                    # A crash after persisting but before acknowledging leaves
                    # the file behind.  Acknowledge it on the next pass without
                    # touching a newer request that may have replaced it.
                    _ack_wake(str(wake.get("request_id") or ""))
                else:
                    wake = _peek_wake()
                    if wake:
                        state["pending_wake"] = wake
                        self._save_state(state)
                        _ack_wake(str(wake.get("request_id") or ""))
                intent: WakeIntent = "scheduled"
                reason = "interval"
                request_id = ""
                if wake:
                    raw_intent = str(wake.get("intent") or "event")
                    intent = (
                        raw_intent
                        if raw_intent in {"scheduled", "event", "immediate", "manual"}
                        else "event"
                    )  # type: ignore[assignment]
                    reason = str(wake.get("reason") or "external event")
                    request_id = str(wake.get("request_id") or "")
                elif now < next_due:
                    await asyncio.sleep(min(5.0, max(0.25, next_due - now)))
                    continue

                if not config.heartbeat_enabled:
                    self._record_status(state, "disabled", "heartbeat_disabled")
                    await asyncio.sleep(5.0)
                    continue
                runtime_state = self.runtime.engine.runtime()
                if not config.enabled or runtime_state.get("paused"):
                    self._record_status(
                        state,
                        "disabled",
                        "plugin_disabled" if not config.enabled else "agency_paused",
                    )
                    await asyncio.sleep(5.0)
                    continue
                if intent != "manual" and not _active_at(now, config):
                    state["next_due_at"] = self._next_scheduled(now, config)
                    self._record_status(state, "skipped", "outside_active_hours")
                    await asyncio.sleep(
                        min(5.0, max(0.25, float(state["next_due_at"]) - time.time()))
                    )
                    continue

                recent = [
                    parsed
                    for item in state.get("recent_starts", [])
                    if (parsed := _finite_float(item, -1.0)) >= 0
                ]
                defer = should_defer_wake(
                    intent=intent,
                    now=now,
                    next_due=next_due,
                    last_started=_finite_float(state["last_started_at"])
                    if state.get("last_started_at")
                    else None,
                    recent_starts=recent,
                    min_spacing=config.heartbeat_min_spacing_seconds,
                    flood_window=config.heartbeat_flood_window_seconds,
                    flood_threshold=config.heartbeat_flood_threshold,
                )
                if defer:
                    self._record_status(state, "deferred", defer)
                    await asyncio.sleep(1.0)
                    continue
                # Core user/session work always wins. The option controls only
                # additional background-work deferral, matching OpenClaw's split.
                if self._gateway_busy(gateway) or (
                    config.heartbeat_skip_when_busy and self._background_busy(gateway)
                ):
                    state["next_due_at"] = min(
                        self._next_scheduled(now, config),
                        now + max(30, config.heartbeat_min_spacing_seconds),
                    )
                    self._record_status(state, "deferred", "gateway_busy", force=True)
                    await asyncio.sleep(1.0)
                    continue

                state["last_request_id"] = request_id
                if wake and request_id:
                    state["claimed_wake"] = dict(wake)
                    state.pop("pending_wake", None)
                # Advance the schedule before model execution. A process crash or
                # ambiguous adapter send can no longer restart the same heartbeat
                # immediately and duplicate its output.
                state["next_due_at"] = self._next_scheduled(now, config)
                self._save_state(state)
                executed = await self.run_once(
                    gateway,
                    config,
                    state,
                    reason=reason,
                    request_id=request_id,
                )
                completed = time.time()
                if executed:
                    state["runs"] = _nonnegative_int(state.get("runs")) + 1
                    state["consecutive_failures"] = 0
                    delivery_status = str((state.get("delivery") or {}).get("status") or "")
                    if intent != "scheduled" and delivery_status in {
                        "delivered",
                        "silent",
                        "suppressed",
                        "ambiguous",
                    }:
                        state["next_due_at"] = self._next_scheduled_after_wake(completed, config)
                elif state.get("last_status") == "failed":
                    state["consecutive_failures"] = (
                        _nonnegative_int(state.get("consecutive_failures")) + 1
                    )
                    state["next_due_at"] = completed + self._failure_delay(state)
                self._save_state(state)
                if state.get("last_reason") == "no_main_session" and state.get("pending_wake"):
                    await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Conscious Agency heartbeat loop failed")
                delay = 5.0
                with contextlib.suppress(Exception):
                    recovery = state if isinstance(state, dict) else self._load_state()
                    self._restore_unstarted_wake(recovery)
                    self._finalize_exception(recovery, exc)
                    delay = min(60.0, max(5.0, self._failure_delay(recovery)))
                await asyncio.sleep(delay)
        logger.info("Conscious Agency native heartbeat runner stopped")
        self._mark_runner_stopped()

    def _due_commitments(self, state: dict[str, Any], now: float) -> list[dict[str, Any]]:
        last_runs = dict(state.get("commitment_last_runs") or {})
        active = self.store.list_intentions("active", 100)
        active_ids = {str(item.get("id") or "") for item in active}
        state["commitment_last_runs"] = {
            item_id: value for item_id, value in last_runs.items() if item_id in active_ids
        }
        due: list[tuple[float, int, dict[str, Any]]] = []
        for item in active:
            raw_due = str(item.get("due_at") or "")
            if not raw_due:
                continue
            try:
                parsed = datetime.fromisoformat(raw_due)
                parsed = (
                    parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
                )
            except (TypeError, ValueError):
                continue
            item_id = str(item.get("id") or "")
            revision = str(item.get("updated_at") or "")
            previous = last_runs.get(item_id)
            previous_revision = (
                str(previous.get("revision") or "") if isinstance(previous, dict) else ""
            )
            # Legacy scalar entries mean the old revision ran once. Preserve that
            # behavior until the intention is edited/reactivated, then run it again.
            already_ran = previous is not None and (
                not isinstance(previous, dict) or previous_revision == revision
            )
            if parsed.timestamp() > now or already_ran:
                continue
            due.append((parsed.timestamp(), -int(item.get("priority") or 0), item))
        due.sort(key=lambda candidate: (candidate[0], candidate[1]))
        return [item for _, _, item in due[:3]]

    def _preflight(
        self, config: AgencyConfig, state: dict[str, Any], now: float
    ) -> tuple[str, list[str], list[str], str]:
        path = hermes_home() / "HEARTBEAT.md"
        content: str | None = None
        if path.is_file():
            content = path.read_text(encoding="utf-8")
        tasks = parse_heartbeat_tasks(content or "")
        known_task_names = {task.name for task in tasks}
        task_state = {
            name: parsed
            for name, value in dict(state.get("task_last_runs") or {}).items()
            if name in known_task_names and (parsed := _finite_float(value, -1.0)) >= 0
        }
        state["task_last_runs"] = task_state
        due_tasks = [
            task
            for task in tasks
            if is_task_due(
                task_state.get(task.name),
                task.interval,
                now,
            )
        ]
        commitments = self._due_commitments(state, now)
        if config.heartbeat_target == "none":
            # Internal-only heartbeats cannot satisfy a user-facing reminder.
            # Leave it due for the next deliverable run instead of silently
            # consuming it while outbound delivery is disabled.
            commitments = []
        if content is not None and heartbeat_content_effectively_empty(content) and not commitments:
            return "", [], [], "empty_heartbeat_file"
        if tasks and not due_tasks and not commitments:
            return "", [], [], "no_tasks_due"
        sections = [] if unrestricted_subjective_heartbeat(config) else [HEARTBEAT_PROMPT]
        directives = _content_without_tasks(content or "")
        if directives:
            sections.append(f"HEARTBEAT.md:\n{directives}")
        if due_tasks:
            sections.append(
                "Due heartbeat tasks:\n"
                + "\n".join(f"- {task.name}: {task.prompt}" for task in due_tasks)
            )
        if commitments:
            sections.append(
                "Due persistent commitments:\n"
                + "\n".join(
                    f"- [{item.get('id')}] {str(item.get('title') or '')[:400]}"
                    for item in commitments
                )
            )
        return (
            "\n\n".join(sections),
            [task.name for task in due_tasks],
            [str(item.get("id")) for item in commitments],
            "",
        )

    @staticmethod
    def _target_entry(gateway: Any, config: AgencyConfig) -> Any | None:
        try:
            entries = gateway.session_store.list_sessions()
        except Exception:
            return None
        for entry in entries:
            source = getattr(entry, "origin", None)
            platform = getattr(source, "platform", None)
            if source is None or platform is None:
                continue
            is_local = getattr(platform, "value", "local") == "local"
            if is_local and config.heartbeat_target != "none":
                continue
            try:
                if not is_local and gateway._adapter_for_source(source) is None:
                    continue
            except Exception:
                continue
            return entry
        return None

    @staticmethod
    async def _raw_transcript(gateway: Any, session_id: str) -> list[dict[str, Any]]:
        """Read the canonical transcript without alternation repair."""

        store = getattr(gateway, "session_store", None)
        database = getattr(store, "_db", None)
        raw_load = getattr(database, "get_messages_as_conversation", None)
        if callable(raw_load):
            result = raw_load(session_id, repair_alternation=False)
            if inspect.isawaitable(result):
                result = await result
            return [dict(item) for item in (result or []) if isinstance(item, dict)]
        load = getattr(store, "load_transcript", None)
        if not callable(load):
            raise RuntimeError("Hermes transcript loading is unavailable")
        result = load(session_id)
        if inspect.isawaitable(result):
            result = await result
        return [dict(item) for item in (result or []) if isinstance(item, dict)]

    @staticmethod
    async def _rewrite_transcript(
        gateway: Any, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        store = getattr(gateway, "session_store", None)
        rewrite = getattr(store, "rewrite_transcript", None)
        if not callable(rewrite):
            raise RuntimeError("Hermes transcript rewrite is unavailable")
        result = rewrite(session_id, messages)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise RuntimeError("Hermes refused transcript reconciliation")

    async def _restore_heartbeat_baseline(self, gateway: Any, turn: HeartbeatTurn) -> None:
        if not turn.baseline_captured:
            return
        current = await self._raw_transcript(gateway, turn.target_session_id)
        if current == turn.baseline_transcript:
            return
        await self._rewrite_transcript(
            gateway,
            turn.target_session_id,
            [dict(item) for item in turn.baseline_transcript],
        )
        restored = await self._raw_transcript(gateway, turn.target_session_id)
        if restored != turn.baseline_transcript:
            raise RuntimeError("Heartbeat trigger leaked into the durable transcript")

    @staticmethod
    def _sanitize_cached_heartbeat_messages(
        gateway: Any,
        turn: HeartbeatTurn,
        *,
        note: dict[str, Any] | None = None,
        assistant: dict[str, Any] | None = None,
    ) -> None:
        agent = getattr(gateway, "_running_agents", {}).get(turn.session_key)
        messages = getattr(agent, "_session_messages", None)
        if not isinstance(messages, list):
            return
        marker_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], dict)
                and messages[index].get("role") == "user"
                and messages[index].get("content") == HEARTBEAT_TRANSCRIPT_PROMPT
            ),
            None,
        )
        if marker_index is not None:
            del messages[marker_index:]
        if note is not None:
            messages.append({**note, "_db_persisted": True})
        if assistant is not None:
            messages.append({**assistant, "_db_persisted": True})
        for name, value in (
            ("_persist_user_message_idx", None),
            ("_persist_user_message_override", None),
            ("_persist_user_message_timestamp", None),
        ):
            with contextlib.suppress(Exception):
                setattr(agent, name, value)

    async def _commit_conversation_output(
        self,
        gateway: Any,
        source: Any,
        turn: HeartbeatTurn,
        response: str,
    ) -> None:
        """Commit and deliver one assistant-initiated turn under Hermes' session lease."""

        clean = str(response or "").strip()
        turn.visible_output = clean
        silent = not clean or clean.casefold() in {"[silent]", HEARTBEAT_OK.casefold()}
        with turn.response_lock:
            if turn.interrupted_by_user:
                turn.delivery_status = "interrupted"
                return
            if silent:
                turn.delivery_status = "silent"
                return
            if not turn.delivery_enabled:
                turn.delivery_status = "suppressed"
                return
            # From this point a real inbound event queues behind the ordered
            # transcript+adapter commit instead of attempting to recall it.
            turn.delivery_started = True

        try:
            await self._restore_heartbeat_baseline(gateway, turn)
            observed_at = time.time()
            local_time = datetime.fromtimestamp(
                observed_at, ZoneInfo(load_config().timezone)
            ).isoformat()
            note = {
                "role": "system",
                "content": f"{HEARTBEAT_TRANSCRIPT_NOTE} Recorded at {local_time}.",
                "timestamp": observed_at,
            }
            assistant = {
                "role": "assistant",
                "content": clean,
                "timestamp": observed_at,
            }
            store = getattr(gateway, "session_store", None)
            append = getattr(store, "append_to_transcript", None)
            if not callable(append):
                raise RuntimeError("Hermes transcript append is unavailable")
            for message in (note, assistant):
                result = append(turn.target_session_id, message)
                if inspect.isawaitable(result):
                    await result
            committed = await self._raw_transcript(gateway, turn.target_session_id)
            if len(committed) < 2 or committed[-2:] != [note, assistant]:
                raise RuntimeError("Hermes did not persist the assistant-initiated turn")
            self._sanitize_cached_heartbeat_messages(gateway, turn, note=note, assistant=assistant)
            refresh = getattr(gateway, "_refresh_agent_cache_message_count", None)
            if callable(refresh):
                refreshed = refresh(turn.session_key, turn.target_session_id)
                if inspect.isawaitable(refreshed):
                    await refreshed
            turn.transcript_committed = True
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._restore_heartbeat_baseline(gateway, turn)
            self._sanitize_cached_heartbeat_messages(gateway, turn)
            turn.delivery_status = "failed"
            turn.delivery_error = f"transcript_commit_{type(exc).__name__}"
            self._set_turn_decision_status(turn, "failed_transcript_commit")
            state = turn.state or {}
            state["delivery"] = {
                **dict(state.get("delivery") or {}),
                "status": "failed",
                "finished_at": time.time(),
            }
            self._save_state(state)
            return

        state = turn.state or {}
        state["delivery"] = {
            "run_id": turn.run_id,
            "status": "sending",
            "started_at": time.time(),
            "target_session_id": turn.target_session_id,
            "message_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        }
        self._save_state(state)
        adapter = gateway._adapter_for_source(source)
        if adapter is None:
            turn.delivery_status = "failed"
            turn.delivery_error = "target_adapter_unavailable"
            self._set_turn_decision_status(turn, "failed_adapter_unavailable")
            state["delivery"]["status"] = "failed"
            state["delivery"]["finished_at"] = time.time()
            self._save_state(state)
            return
        metadata: dict[str, Any] = {}
        if getattr(source, "thread_id", None):
            metadata["thread_id"] = source.thread_id
        try:
            send_result = await adapter.send(str(source.chat_id), clean, metadata=metadata or None)
        except Exception as exc:
            turn.delivery_status = "ambiguous"
            turn.delivery_error = f"adapter_send_{type(exc).__name__}"
            self._set_turn_decision_status(turn, "ambiguous")
            state["delivery"]["status"] = "ambiguous"
            state["delivery"]["finished_at"] = time.time()
            self._save_state(state)
            return
        if not bool(getattr(send_result, "success", False)):
            turn.delivery_status = "ambiguous"
            turn.delivery_error = "adapter_send_unconfirmed"
            self._set_turn_decision_status(turn, "ambiguous")
            state["delivery"]["status"] = "ambiguous"
            state["delivery"]["finished_at"] = time.time()
            self._save_state(state)
            return
        turn.delivery_status = "delivered"
        self._set_turn_decision_status(turn, "delivered")
        state["delivery"]["status"] = "delivered"
        state["delivery"]["finished_at"] = time.time()
        self._save_state(state)

    async def _drain_deferred_user_events(self, gateway: Any, turn: HeartbeatTurn) -> None:
        with turn.response_lock:
            turn.accepting_user_events = False
            events = list(turn.deferred_user_events)
            turn.deferred_user_events.clear()
        _unregister_active_heartbeat(turn)
        for event in events:
            try:
                source = getattr(event, "source", None)
                adapter = gateway._adapter_for_source(source)
                session_key_for = getattr(gateway, "_session_key_for_source", None)
                process = getattr(adapter, "_process_message_background", None)
                if callable(process) and callable(session_key_for):
                    await process(event, session_key_for(source))
                    continue
                response = await gateway._handle_message(event)
                if response and adapter is not None:
                    metadata = (
                        {"thread_id": source.thread_id}
                        if getattr(source, "thread_id", None)
                        else None
                    )
                    await adapter.send(str(source.chat_id), str(response), metadata=metadata)
            except Exception:
                logger.exception("Deferred user turn failed after heartbeat handoff")

    async def run_once(
        self,
        gateway: Any,
        config: AgencyConfig,
        state: dict[str, Any],
        *,
        reason: str,
        request_id: str = "",
    ) -> bool:
        async with self._run_lock:
            now = time.time()
            prompt, due_tasks, due_commitments, skip = self._preflight(config, state, now)
            if skip:
                self._consume_claimed_wake(state, request_id)
                self.runtime.store.add_event(
                    "heartbeat_skipped", summary=skip, metadata={"reason": reason[:200]}
                )
                self._record_status(state, "skipped", skip, force=True)
                return False
            entry = self._target_entry(gateway, config)
            if entry is None or getattr(entry, "origin", None) is None:
                self._restore_unstarted_wake(state)
                self._record_status(state, "skipped", "no_main_session", force=True)
                return False
            source = dataclasses.replace(entry.origin)
            session_id = str(getattr(entry, "session_id", "") or "")
            session_key = str(getattr(entry, "session_key", "") or "")
            run_id = uuid.uuid4().hex
            turn = HeartbeatTurn(
                run_id=run_id,
                prompt=prompt,
                target_session_id=session_id,
                session_key=session_key,
                delivery_enabled=config.heartbeat_target != "none",
                state=state,
            )
            recent = [
                parsed
                for item in state.get("recent_starts", [])
                if (parsed := _finite_float(item, -1.0))
                >= now - config.heartbeat_flood_window_seconds
            ]
            recent.append(now)
            state["recent_starts"] = recent[-(config.heartbeat_flood_threshold + 1) :]
            state["last_started_at"] = now
            state["last_run_id"] = run_id
            state["attempts"] = _nonnegative_int(state.get("attempts")) + 1
            state["inflight"] = {
                "run_id": run_id,
                "started_at": now,
                "target_session_id": session_id,
                "due_tasks": list(due_tasks),
                "due_commitments": list(due_commitments),
                "wake_request_id": request_id,
            }
            state["delivery"] = {
                "run_id": run_id,
                "status": "pending",
                "target_session_id": session_id,
            }
            self._consume_claimed_wake(state, request_id)
            self._record_status(state, "running", "model_turn", force=True)
            self.runtime.store.add_event(
                "heartbeat_started",
                session_id=session_id,
                summary="Native heartbeat turn started",
                metadata={
                    "run_id": run_id,
                    "reason": reason[:200],
                    "due_tasks": due_tasks,
                    "due_commitments": due_commitments,
                },
            )
            response = ""
            _register_active_heartbeat(turn, source, session_key)
            try:
                try:
                    from gateway.platforms.base import MessageEvent

                    event = MessageEvent(
                        text=HEARTBEAT_TRANSCRIPT_PROMPT,
                        source=source,
                        internal=True,
                        metadata={
                            "agency_heartbeat": True,
                            "agency_heartbeat_run_id": run_id,
                        },
                    )
                    turn.baseline_transcript = await self._raw_transcript(gateway, session_id)
                    turn.baseline_captured = True
                    with heartbeat_turn(turn):
                        handler_result = await asyncio.wait_for(
                            gateway._handle_message(event),
                            timeout=config.heartbeat_timeout_seconds,
                        )
                        if turn.delivery_status == "pending":
                            fallback_response = str(handler_result or "").strip()
                            if not turn.transformed and callable(
                                getattr(self.runtime, "transform_llm_output", None)
                            ):
                                transformed = self.runtime.transform_llm_output(
                                    fallback_response,
                                    session_id=session_id,
                                    platform=str(
                                        getattr(source.platform, "value", source.platform)
                                    ),
                                )
                                if isinstance(transformed, str):
                                    fallback_response = transformed.strip()
                            await self._commit_conversation_output(
                                gateway, source, turn, fallback_response
                            )
                    response = turn.visible_output
                except TimeoutError:
                    running = getattr(gateway, "_running_agents", {}).get(turn.session_key)
                    if running is not None and hasattr(running, "interrupt"):
                        with contextlib.suppress(Exception):
                            running.interrupt("native heartbeat timeout")
                    self._close_inflight(state, time.time(), consume_due=False)
                    self._set_turn_decision_status(turn, "failed_timeout")
                    state["delivery"] = {
                        **dict(state.get("delivery") or {}),
                        "status": "failed",
                        "finished_at": time.time(),
                    }
                    self._record_status(state, "failed", "timeout")
                    self.runtime.store.add_event(
                        "heartbeat_failed",
                        session_id=session_id,
                        summary="Heartbeat timed out",
                        metadata={"run_id": run_id, "error_type": "TimeoutError"},
                    )
                    with contextlib.suppress(Exception):
                        await self._restore_heartbeat_baseline(gateway, turn)
                    self._sanitize_cached_heartbeat_messages(gateway, turn)
                    await self._drain_deferred_user_events(gateway, turn)
                    return False

                if turn.decision_id and isinstance(state.get("inflight"), dict):
                    state["inflight"]["decision_id"] = turn.decision_id
                    state["delivery"]["decision_id"] = turn.decision_id
                with turn.response_lock:
                    interrupted = turn.interrupted_by_user

                if interrupted:
                    self._close_inflight(state, time.time(), consume_due=False)
                    self._set_turn_decision_status(turn, "interrupted")
                    state["delivery"] = {
                        **dict(state.get("delivery") or {}),
                        "status": "interrupted",
                        "finished_at": time.time(),
                    }
                    self._record_status(state, "interrupted", "real_user_message")
                    self.runtime.store.add_event(
                        "heartbeat_interrupted",
                        session_id=session_id,
                        summary="Native heartbeat yielded to a real user message",
                        metadata={"run_id": run_id},
                    )
                    await self._drain_deferred_user_events(gateway, turn)
                    return True
                delivery_status = turn.delivery_status
                if delivery_status == "silent":
                    self._set_turn_decision_status(turn, "silent")
                    state["delivery"] = {
                        **dict(state.get("delivery") or {}),
                        "status": "silent",
                        "finished_at": time.time(),
                    }
                elif delivery_status == "suppressed":
                    self._set_turn_decision_status(turn, "suppressed_target_none")
                    state["delivery"] = {
                        **dict(state.get("delivery") or {}),
                        "status": "suppressed",
                        "finished_at": time.time(),
                    }
                elif delivery_status == "failed":
                    self._close_inflight(state, time.time(), consume_due=False)
                    self._record_status(
                        state,
                        "failed",
                        turn.delivery_error or "transcript_commit_failed",
                    )
                    self.runtime.store.add_event(
                        "heartbeat_failed",
                        session_id=session_id,
                        summary="Heartbeat output could not be committed",
                        metadata={"run_id": run_id, "error_type": turn.delivery_error},
                    )
                    await self._drain_deferred_user_events(gateway, turn)
                    return False
                elif delivery_status == "ambiguous":
                    self._close_inflight(state, time.time(), consume_due=True)
                    self._record_status(state, "failed", "ambiguous_delivery")
                    self.runtime.store.add_event(
                        "heartbeat_delivery_reconciled",
                        session_id=session_id,
                        summary="Heartbeat delivery outcome is ambiguous and will not replay",
                        metadata={"run_id": run_id, "delivery_status": "ambiguous"},
                    )
                    await self._drain_deferred_user_events(gateway, turn)
                    return True
                elif delivery_status != "delivered":
                    self._close_inflight(state, time.time(), consume_due=False)
                    self._record_status(state, "failed", "missing_delivery_commit")
                    await self._drain_deferred_user_events(gateway, turn)
                    return False

                completed = time.time()
                self._close_inflight(state, completed, consume_due=True)
                final_status = delivery_status
                self._record_status(state, final_status, "")
                self.runtime.store.add_event(
                    "heartbeat_finished",
                    session_id=session_id,
                    summary="Native heartbeat turn finished",
                    metadata={
                        "run_id": run_id,
                        "delivered": delivery_status == "delivered",
                        "message_chars": len(response),
                    },
                )
                await self._drain_deferred_user_events(gateway, turn)
                return True
            finally:
                _unregister_active_heartbeat(turn)


_PATCH_LOCK = threading.Lock()


def _patch_display_settings() -> None:
    try:
        import gateway.display_config as display_config
    except Exception:
        return
    original = getattr(display_config, "resolve_display_setting", None)
    if not callable(original) or getattr(original, "_agency_heartbeat_patch", False):
        return

    def wrapped(config: Any, platform_key: str, name: str, *args: Any, **kwargs: Any):
        if current_heartbeat_turn() is not None and name in {
            "streaming",
            "interim_assistant_messages",
            "tool_progress",
            "long_running_notifications",
            "show_reasoning",
        }:
            return False
        return original(config, platform_key, name, *args, **kwargs)

    wrapped._agency_heartbeat_patch = True  # type: ignore[attr-defined]
    display_config.resolve_display_setting = wrapped


def _patch_agent_persistence() -> bool:
    """Keep the API-only heartbeat trigger out of Hermes' durable transcript."""

    try:
        from run_agent import AIAgent
    except Exception:
        return False
    if getattr(AIAgent, "_agency_assistant_turn_persistence_patch", False):
        return True
    original_persist = getattr(AIAgent, "_persist_session", None)
    original_flush = getattr(AIAgent, "_flush_messages_to_session_db", None)
    if not callable(original_persist) or not callable(original_flush):
        return False

    def persist(agent: Any, messages: Any, conversation_history: Any = None):
        turn = current_heartbeat_turn()
        if turn is not None and str(getattr(agent, "session_id", "") or "") == str(
            turn.target_session_id
        ):
            # Hermes calls persistence at turn start, after tool rounds, and
            # before transform_llm_output. The gateway wrapper below performs
            # the sole final commit after Agency has selected the exact visible
            # assistant output.
            with contextlib.suppress(Exception):
                from agent.agent_runtime_helpers import note_turn_persisted

                note_turn_persisted(agent)
            return None
        return original_persist(agent, messages, conversation_history)

    def flush(agent: Any, messages: Any, conversation_history: Any = None):
        turn = current_heartbeat_turn()
        if turn is not None and str(getattr(agent, "session_id", "") or "") == str(
            turn.target_session_id
        ):
            return None
        return original_flush(agent, messages, conversation_history)

    persist._agency_assistant_turn_persistence_patch = True  # type: ignore[attr-defined]
    flush._agency_assistant_turn_persistence_patch = True  # type: ignore[attr-defined]
    AIAgent._persist_session = persist
    AIAgent._flush_messages_to_session_db = flush
    AIAgent._agency_assistant_turn_persistence_patch = True
    return True


def _patch_gateway(runtime: Any) -> bool:
    module = sys.modules.get("gateway.run")
    runner_class = getattr(module, "GatewayRunner", None) if module else None
    if runner_class is None:
        return False
    with _PATCH_LOCK:
        if getattr(runner_class, "_agency_heartbeat_patch", False):
            runner_class._agency_heartbeat_runtime = runtime
            _patch_agent_persistence()
            _patch_display_settings()
            return True
        original_startup = getattr(runner_class, "_finish_startup_restore", None)
        original_handler = getattr(runner_class, "_handle_message_with_agent", None)
        if (
            not callable(original_startup)
            or not callable(original_handler)
            or not _patch_agent_persistence()
        ):
            return False

        async def wrapped_startup(gateway: Any, *args: Any, **kwargs: Any):
            result = await original_startup(gateway, *args, **kwargs)
            active_runtime = getattr(type(gateway), "_agency_heartbeat_runtime", runtime)
            active_runtime.ensure_heartbeat(gateway)
            return result

        async def wrapped_handler(
            gateway: Any,
            event: Any,
            source: Any,
            quick_key: str,
            run_generation: int,
        ):
            turn = current_heartbeat_turn()
            metadata = dict(getattr(event, "metadata", None) or {})
            if turn is None or metadata.get("agency_heartbeat") is not True:
                return await original_handler(gateway, event, source, quick_key, run_generation)
            runner = getattr(gateway, "_conscious_agency_heartbeat_runner", None)
            if not bool(getattr(runner, "_agency_heartbeat_runner", False)):
                raise RuntimeError("Conscious Agency heartbeat runner is unavailable")
            turn.baseline_transcript = await runner._raw_transcript(gateway, turn.target_session_id)
            turn.baseline_captured = True
            result = await original_handler(gateway, event, source, quick_key, run_generation)
            if turn.interrupted_by_user:
                await runner._restore_heartbeat_baseline(gateway, turn)
                runner._sanitize_cached_heartbeat_messages(gateway, turn)
                turn.delivery_status = "interrupted"
                return None
            response = str(result or "").strip()
            if not turn.transformed and callable(
                getattr(runner.runtime, "transform_llm_output", None)
            ):
                transformed = runner.runtime.transform_llm_output(
                    response,
                    session_id=turn.target_session_id,
                    platform=str(getattr(source.platform, "value", source.platform)),
                )
                if isinstance(transformed, str):
                    response = transformed.strip()
            await runner._commit_conversation_output(gateway, source, turn, response)
            # Delivery is owned by _commit_conversation_output. Returning the
            # body here would hand it to a second adapter-send owner.
            return None

        runner_class._finish_startup_restore = wrapped_startup
        runner_class._handle_message_with_agent = wrapped_handler
        runner_class._agency_heartbeat_patch = True
        runner_class._agency_heartbeat_runtime = runtime
        _patch_display_settings()
    return True


def arm_gateway_integration(runtime: Any) -> None:
    """Attach after GatewayRunner's class exists during Hermes' circular startup import."""

    if _patch_gateway(runtime):
        return
    command = " ".join(sys.argv).casefold()
    if "gateway" not in command:
        return

    def wait_for_gateway() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _patch_gateway(runtime):
                return
            time.sleep(0.01)
        logger.error("Conscious Agency could not attach the native heartbeat to GatewayRunner")

    threading.Thread(
        target=wait_for_gateway,
        daemon=True,
        name="conscious-agency-heartbeat-attach",
    ).start()
