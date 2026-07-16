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
HEARTBEAT_TRANSCRIPT_PROMPT = "[Hermes heartbeat poll]"
HEARTBEAT_PROMPT = (
    "Read HEARTBEAT.md if it exists in the Hermes home. Follow it strictly. "
    "Do not infer or repeat old tasks from prior chats. Use heartbeat_respond with notify=false "
    "when nothing needs the user's attention, or notify=true with notification_text when the "
    "user should be interrupted."
)
_WAKE_FILE = "heartbeat-wake.json"
_STATE_KEY = "heartbeat_state"
_MAX_ACTIVE_SEEK = timedelta(days=7)

WakeIntent = Literal["scheduled", "event", "immediate", "manual"]


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
    response: dict[str, Any] | None = None
    raw_output: str = ""
    transformed: bool = False
    interrupted_by_user: bool = False
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


def _register_active_heartbeat(turn: HeartbeatTurn, source: Any) -> None:
    key = _source_key(source)
    turn.target_source_key = key
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


def release_heartbeat_for_user_turn(event: Any) -> HeartbeatTurn | None:
    """Detach a recursively processed real user event from its heartbeat parent."""

    if event is None or bool(getattr(event, "internal", False)):
        return None
    current = current_heartbeat_turn()
    key = _source_key(getattr(event, "source", None))
    with _active_heartbeat_lock:
        active = _active_heartbeats.get(key) if key is not None else None
    turn = active or (
        current
        if current is not None
        and (current.target_source_key is None or current.target_source_key == key)
        else None
    )
    if turn is None:
        return None
    with turn.response_lock:
        turn.interrupted_by_user = True
    if current is turn:
        _heartbeat_turn.set(None)
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
    text = str(notification_text or "").strip()
    if notify and not text:
        raise ValueError("notification_text is required when notify=true")
    # Parallel tool batches and confused models can submit the decision more than once.
    # The first valid decision is authoritative; later calls cannot replace it.
    with turn.response_lock:
        if turn.response is None:
            turn.response = {"notify": bool(notify), "notification_text": text}
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
    for index, line in enumerate(lines):
        trimmed = line.strip()
        if trimmed == "tasks:":
            in_tasks = True
            continue
        if not in_tasks:
            continue
        if trimmed and not line[:1].isspace() and not trimmed.startswith("-"):
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
    for line in lines:
        trimmed = line.strip()
        if trimmed == "tasks:" and not line[:1].isspace():
            in_tasks = True
            continue
        if in_tasks:
            if trimmed and not line[:1].isspace() and not trimmed.startswith("-"):
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
    candidate = start
    horizon = start + _MAX_ACTIVE_SEEK.total_seconds()
    interval = max(float(interval_seconds), 1.0)
    step = interval * max(1, math.ceil(30.0 / interval))
    while candidate < horizon:
        if _active_at(candidate, config):
            return candidate
        candidate += step
    return start


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


def request_heartbeat_wake(intent: WakeIntent = "manual", reason: str = "operator") -> str:
    request_id = uuid.uuid4().hex
    path = _wake_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": request_id,
        "intent": intent,
        "reason": str(reason or "")[:300],
        "requested_at": time.time(),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
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


def _read_wake() -> dict[str, Any] | None:
    path = _wake_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return payload if isinstance(payload, dict) else None


def heartbeat_status(store: AgencyStore | None = None) -> dict[str, Any]:
    config = load_config()
    value = (store or AgencyStore(config)).get_meta(_STATE_KEY, {})
    state = value if isinstance(value, dict) else {}
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
        "next_due_at": state.get("next_due_at"),
        "last_started_at": state.get("last_started_at"),
        "last_completed_at": state.get("last_completed_at"),
        "last_status": state.get("last_status", "never_started"),
        "last_reason": state.get("last_reason", ""),
        "runs": int(state.get("runs") or 0),
        "legacy_cron_removed": bool(state.get("legacy_cron_removed", False)),
    }


class HeartbeatRunner:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store: AgencyStore = runtime.store
        self._gateway_ref: weakref.ReferenceType[Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._run_lock = asyncio.Lock()

    def start(self, gateway: Any) -> asyncio.Task[Any] | None:
        self._gateway_ref = weakref.ref(gateway)
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self.run(gateway),
            name="conscious-agency-heartbeat",
            context=contextvars.Context(),
        )
        return self._task

    def _load_state(self) -> dict[str, Any]:
        value = self.store.get_meta(_STATE_KEY, {})
        state = dict(value) if isinstance(value, dict) else {}
        state.setdefault("recent_starts", [])
        state.setdefault("task_last_runs", {})
        state.setdefault("commitment_last_runs", {})
        state.setdefault("runs", 0)
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.store.set_meta(_STATE_KEY, state)

    def _reconcile_interrupted_run(self) -> None:
        """Close an unfinished run left by a gateway stop or process crash."""

        state = self._load_state()
        started = float(state.get("last_started_at") or 0)
        completed = float(state.get("last_completed_at") or 0)
        if started <= completed:
            return
        if (
            state.get("last_status") == "interrupted"
            and float(state.get("last_interrupted_at") or 0) >= started
        ):
            return
        state["last_status"] = "interrupted"
        state["last_reason"] = "gateway_restart"
        state["last_interrupted_at"] = time.time()
        self._save_state(state)
        self.runtime.store.add_event(
            "heartbeat_interrupted",
            summary="An unfinished heartbeat was closed after gateway restart",
        )

    def _record_status(self, state: dict[str, Any], status: str, reason: str = "") -> None:
        state["last_status"] = status
        state["last_reason"] = str(reason or "")[:500]
        self._save_state(state)

    def _next_scheduled(self, now: float, config: AgencyConfig) -> float:
        interval = _parse_duration(config.heartbeat_every)
        phase = heartbeat_phase_seconds(_scheduler_seed(), "conscious-agency", interval)
        return seek_active_due(next_phase_due(now, interval, phase), interval, config)

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

    async def run(self, gateway: Any) -> None:
        logger.info("Conscious Agency native heartbeat runner started")
        self._reconcile_interrupted_run()
        while bool(getattr(gateway, "_running", False)):
            try:
                config = load_config()
                self.runtime.reload_config(config)
                state = self._load_state()
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
                next_due = float(state.get("next_due_at") or 0)
                if next_due <= 0:
                    next_due = self._next_scheduled(now, config)
                    state["next_due_at"] = next_due
                    self._save_state(state)
                wake = state.get("pending_wake")
                if not isinstance(wake, dict):
                    wake = _read_wake()
                    if wake:
                        state["pending_wake"] = wake
                        self._save_state(state)
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

                recent = [float(item) for item in state.get("recent_starts", [])]
                defer = should_defer_wake(
                    intent=intent,
                    now=now,
                    next_due=next_due,
                    last_started=float(state["last_started_at"])
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
                if config.heartbeat_skip_when_busy and self._gateway_busy(gateway):
                    state["next_due_at"] = min(
                        self._next_scheduled(now, config),
                        now + max(30, config.heartbeat_min_spacing_seconds),
                    )
                    self._record_status(state, "deferred", "gateway_busy")
                    await asyncio.sleep(1.0)
                    continue

                state["last_request_id"] = request_id
                state.pop("pending_wake", None)
                self._save_state(state)
                executed = await self.run_once(gateway, config, state, reason=reason)
                completed = time.time()
                state["next_due_at"] = self._next_scheduled(completed, config)
                if executed:
                    state["last_completed_at"] = completed
                    state["runs"] = int(state.get("runs") or 0) + 1
                self._save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Conscious Agency heartbeat loop failed")
                with contextlib.suppress(Exception):
                    state = self._load_state()
                    state["next_due_at"] = time.time() + 30
                    self._record_status(state, "failed", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(5.0)
        logger.info("Conscious Agency native heartbeat runner stopped")

    def _due_commitments(self, state: dict[str, Any], now: float) -> list[dict[str, Any]]:
        last_runs = dict(state.get("commitment_last_runs") or {})
        due: list[dict[str, Any]] = []
        for item in self.store.list_intentions("active", 50):
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
            if parsed.timestamp() > now or str(item.get("id")) in last_runs:
                continue
            due.append(item)
        return due[:3]

    def _preflight(
        self, config: AgencyConfig, state: dict[str, Any], now: float
    ) -> tuple[str, list[str], list[str], str]:
        path = hermes_home() / "HEARTBEAT.md"
        content: str | None = None
        if path.is_file():
            content = path.read_text(encoding="utf-8")
        tasks = parse_heartbeat_tasks(content or "")
        task_state = dict(state.get("task_last_runs") or {})
        due_tasks = [
            task
            for task in tasks
            if is_task_due(
                float(task_state[task.name]) if task.name in task_state else None,
                task.interval,
                now,
            )
        ]
        commitments = self._due_commitments(state, now)
        if content is not None and heartbeat_content_effectively_empty(content) and not commitments:
            return "", [], [], "empty_heartbeat_file"
        if tasks and not due_tasks and not commitments:
            return "", [], [], "no_tasks_due"
        sections = [HEARTBEAT_PROMPT]
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
    def _target_entry(gateway: Any) -> Any | None:
        try:
            entries = gateway.session_store.list_sessions()
        except Exception:
            return None
        for entry in entries:
            source = getattr(entry, "origin", None)
            platform = getattr(source, "platform", None)
            if source is None or platform is None or getattr(platform, "value", "local") == "local":
                continue
            try:
                if gateway._adapter_for_source(source) is None:
                    continue
            except Exception:
                continue
            return entry
        return None

    async def run_once(
        self,
        gateway: Any,
        config: AgencyConfig,
        state: dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        async with self._run_lock:
            now = time.time()
            prompt, due_tasks, due_commitments, skip = self._preflight(config, state, now)
            if skip:
                self.runtime.store.add_event(
                    "heartbeat_skipped", summary=skip, metadata={"reason": reason[:200]}
                )
                self._record_status(state, "skipped", skip)
                return False
            entry = self._target_entry(gateway)
            if entry is None or getattr(entry, "origin", None) is None:
                self._record_status(state, "skipped", "no_main_session")
                return False
            source = dataclasses.replace(entry.origin)
            session_id = str(getattr(entry, "session_id", "") or "")
            session_key = str(getattr(entry, "session_key", "") or "")
            previous_updated_at = getattr(entry, "updated_at", None)
            run_id = uuid.uuid4().hex
            turn = HeartbeatTurn(run_id=run_id, prompt=prompt, target_session_id=session_id)
            recent = [
                float(item)
                for item in state.get("recent_starts", [])
                if float(item) >= now - config.heartbeat_flood_window_seconds
            ]
            recent.append(now)
            state["recent_starts"] = recent[-(config.heartbeat_flood_threshold + 1) :]
            state["last_started_at"] = now
            self._save_state(state)
            self.runtime.store.add_event(
                "heartbeat_started",
                session_id=session_id,
                summary="Native heartbeat turn started",
                metadata={
                    "reason": reason[:200],
                    "due_tasks": due_tasks,
                    "due_commitments": due_commitments,
                },
            )
            response = ""
            _register_active_heartbeat(turn, source)
            try:
                from gateway.platforms.base import MessageEvent

                event = MessageEvent(
                    text=HEARTBEAT_TRANSCRIPT_PROMPT,
                    source=source,
                    internal=True,
                    metadata={
                        "agency_heartbeat": True,
                        "gateway_session_id": session_id,
                    },
                )
                with heartbeat_turn(turn):
                    response = str(
                        await asyncio.wait_for(
                            gateway._handle_message(event),
                            timeout=config.heartbeat_timeout_seconds,
                        )
                        or ""
                    ).strip()
                    if turn.interrupted_by_user:
                        response = ""
                    elif not turn.transformed and callable(
                        getattr(self.runtime, "transform_llm_output", None)
                    ):
                        transformed = self.runtime.transform_llm_output(
                            response,
                            session_id=session_id,
                            platform=str(getattr(source.platform, "value", source.platform)),
                        )
                        if isinstance(transformed, str):
                            response = transformed.strip()
            except TimeoutError:
                running = getattr(gateway, "_running_agents", {}).get(session_key)
                if running is not None and hasattr(running, "interrupt"):
                    with contextlib.suppress(Exception):
                        running.interrupt("native heartbeat timeout")
                self._record_status(state, "failed", "timeout")
                self.runtime.store.add_event(
                    "heartbeat_failed", session_id=session_id, summary="Heartbeat timed out"
                )
                return True
            finally:
                if previous_updated_at is not None and not turn.interrupted_by_user:
                    with contextlib.suppress(Exception):
                        entry.updated_at = previous_updated_at
                        await gateway.async_session_store._save()
                _unregister_active_heartbeat(turn)

            if turn.interrupted_by_user:
                self._record_status(state, "interrupted", "real_user_message")
                self.runtime.store.add_event(
                    "heartbeat_interrupted",
                    session_id=session_id,
                    summary="Native heartbeat yielded to a real user message",
                )
                return True

            silent = not response or response.casefold() in {"[silent]", HEARTBEAT_OK.casefold()}
            delivered = False
            if not silent and config.heartbeat_target != "none":
                adapter = gateway._adapter_for_source(source)
                metadata: dict[str, Any] = {}
                if getattr(source, "thread_id", None):
                    metadata["thread_id"] = source.thread_id
                send_result = await adapter.send(
                    str(source.chat_id), response, metadata=metadata or None
                )
                delivered = bool(getattr(send_result, "success", True))
                if not delivered:
                    raise RuntimeError(
                        str(getattr(send_result, "error", "heartbeat delivery failed"))
                    )

            completed = time.time()
            task_state = dict(state.get("task_last_runs") or {})
            for name in due_tasks:
                task_state[name] = completed
            state["task_last_runs"] = task_state
            commitment_state = dict(state.get("commitment_last_runs") or {})
            for item_id in due_commitments:
                commitment_state[item_id] = completed
            state["commitment_last_runs"] = commitment_state
            self._record_status(state, "delivered" if delivered else "silent", "")
            self.runtime.store.add_event(
                "heartbeat_finished",
                session_id=session_id,
                summary="Native heartbeat turn finished",
                metadata={"delivered": delivered, "message_chars": len(response)},
            )
            return True


_PATCH_LOCK = threading.Lock()


def _patch_iteration_budget() -> None:
    """Give native heartbeat turns their own tool-loop budget."""

    module = sys.modules.get("gateway.run")
    original = getattr(module, "_current_max_iterations", None) if module else None
    if not callable(original) or getattr(original, "_agency_heartbeat_patch", False):
        return

    def wrapped() -> int:
        normal = int(original())
        if current_heartbeat_turn() is None:
            return normal
        with contextlib.suppress(Exception):
            return min(normal, load_config().heartbeat_max_iterations)
        return min(normal, 8)

    wrapped._agency_heartbeat_patch = True  # type: ignore[attr-defined]
    module._current_max_iterations = wrapped


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


def _patch_gateway(runtime: Any) -> bool:
    module = sys.modules.get("gateway.run")
    runner_class = getattr(module, "GatewayRunner", None) if module else None
    if runner_class is None:
        return False
    with _PATCH_LOCK:
        if getattr(runner_class, "_agency_heartbeat_patch", False):
            runner_class._agency_heartbeat_runtime = runtime
            _patch_display_settings()
            _patch_iteration_budget()
            return True
        original = getattr(runner_class, "_finish_startup_restore", None)
        if not callable(original):
            return False

        async def wrapped(gateway: Any, *args: Any, **kwargs: Any):
            result = await original(gateway, *args, **kwargs)
            active_runtime = getattr(type(gateway), "_agency_heartbeat_runtime", runtime)
            active_runtime.ensure_heartbeat(gateway)
            return result

        runner_class._finish_startup_restore = wrapped
        runner_class._agency_heartbeat_patch = True
        runner_class._agency_heartbeat_runtime = runtime
        _patch_display_settings()
        _patch_iteration_budget()
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
