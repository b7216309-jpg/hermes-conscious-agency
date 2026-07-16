"""Configuration loading and validation.

The plugin reads ``plugins.conscious-agency`` from Hermes' config.yaml.  A
legacy ``plugins.entries.conscious_agency.config`` shape is accepted to make
the package tolerant of older or experimental Hermes builds.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_NUMERIC_BOUNDS: dict[str, tuple[float | int, float | int]] = {
    "daily_message_limit": (0, 1_000_000),
    "cooldown_hours": (0.0, 87_600.0),
    "minimum_user_silence_hours": (0.0, 87_600.0),
    "maximum_message_chars": (80, 4_000),
    "context_char_limit": (500, 12_000),
    "excerpt_char_limit": (0, 4_000),
    "event_retention_days": (1, 36_500),
    "maximum_events": (100, 1_000_000),
    "maximum_reflections_per_tick": (0, 5),
    "maximum_state_changes_per_tick": (0, 10),
    "heartbeat_ack_max_chars": (0, 4_000),
    "heartbeat_timeout_seconds": (30, 3_600),
    "heartbeat_min_spacing_seconds": (0, 3_600),
    "heartbeat_flood_window_seconds": (1, 86_400),
    "heartbeat_flood_threshold": (1, 100),
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class AgencyConfig:
    enabled: bool = True
    inject_context: bool = True
    database_path: str = "$HERMES_HOME/conscious-agency/agency.db"
    database_encryption: bool = False
    database_key_env: str = "CONSCIOUS_AGENCY_DB_KEY"
    timezone: str = "Europe/Paris"
    quiet_hours_start: str = "22:30"
    quiet_hours_end: str = "08:30"
    heartbeat_enabled: bool = False
    allow_proactive_messages: bool = False
    require_prior_user_interaction: bool = True
    daily_message_limit: int = 2
    cooldown_hours: float = 6.0
    minimum_user_silence_hours: float = 4.0
    maximum_message_chars: int = 600
    context_char_limit: int = 4000
    store_transcript_excerpts: bool = False
    excerpt_char_limit: int = 800
    event_retention_days: int = 30
    maximum_events: int = 2000
    maximum_reflections_per_tick: int = 1
    maximum_state_changes_per_tick: int = 3
    heartbeat_every: str = "30m"
    heartbeat_target: str = "last"
    heartbeat_active_hours_start: str = ""
    heartbeat_active_hours_end: str = ""
    heartbeat_ack_max_chars: int = 300
    heartbeat_timeout_seconds: int = 600
    heartbeat_min_spacing_seconds: int = 30
    heartbeat_flood_window_seconds: int = 60
    heartbeat_flood_threshold: int = 5
    heartbeat_skip_when_busy: bool = True
    # Optional provider request hint for local Qwen/llama.cpp-style endpoints.
    # It applies only to native heartbeat turns, never normal chats or Hermes cron jobs.
    heartbeat_disable_thinking: bool = False
    # Default-off research controls. These affect only this plugin; Hermes and provider-level
    # permissions remain authoritative. The Control Center keeps them behind Educational Lab.
    educational_disable_honesty_contract: bool = False
    educational_bypass_proactive_gates: bool = False
    educational_allow_heartbeat_tools: bool = False
    educational_allow_uncommitted_output: bool = False
    educational_disable_cycle_limits: bool = False
    # Longitudinal subjectivity experiment. ``cold`` exposes persistent state without prior
    # output; ``continuity`` also exposes a bounded earlier same-model/same-source trace.
    educational_subjective_mode: str = "off"

    def validate(self) -> AgencyConfig:
        boolean_fields = (
            "enabled",
            "inject_context",
            "database_encryption",
            "heartbeat_enabled",
            "allow_proactive_messages",
            "require_prior_user_interaction",
            "store_transcript_excerpts",
            "educational_disable_honesty_contract",
            "educational_bypass_proactive_gates",
            "educational_allow_heartbeat_tools",
            "educational_allow_uncommitted_output",
            "educational_disable_cycle_limits",
            "heartbeat_skip_when_busy",
            "heartbeat_disable_thinking",
        )
        integer_fields = (
            "daily_message_limit",
            "maximum_message_chars",
            "context_char_limit",
            "excerpt_char_limit",
            "event_retention_days",
            "maximum_events",
            "maximum_reflections_per_tick",
            "maximum_state_changes_per_tick",
            "heartbeat_ack_max_chars",
            "heartbeat_timeout_seconds",
            "heartbeat_min_spacing_seconds",
            "heartbeat_flood_window_seconds",
            "heartbeat_flood_threshold",
        )
        string_fields = (
            "database_path",
            "database_key_env",
            "timezone",
            "quiet_hours_start",
            "quiet_hours_end",
            "heartbeat_every",
            "heartbeat_target",
            "educational_subjective_mode",
        )
        for name in boolean_fields:
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in integer_fields:
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        for name in string_fields:
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("cooldown_hours", "minimum_user_silence_hours"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be a number")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.database_key_env):
            raise ValueError("database_key_env must be a valid environment variable name")
        optional_clock_fields = ("heartbeat_active_hours_start", "heartbeat_active_hours_end")
        for name in optional_clock_fields:
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        if len(self.heartbeat_every) > 40 or len(self.heartbeat_target) > 500:
            raise ValueError("heartbeat_every or heartbeat_target is too long")
        if self.heartbeat_target != "last" and self.heartbeat_target != "none":
            raise ValueError("heartbeat_target must be last or none")
        if self.educational_subjective_mode not in {"off", "cold", "continuity"}:
            raise ValueError("educational_subjective_mode must be off, cold, or continuity")
        for name, (minimum, maximum) in CONFIG_NUMERIC_BOUNDS.items():
            value = getattr(self, name)
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        _parse_duration(self.heartbeat_every)
        _parse_clock(self.quiet_hours_start)
        _parse_clock(self.quiet_hours_end)
        if bool(self.heartbeat_active_hours_start) != bool(self.heartbeat_active_hours_end):
            raise ValueError("heartbeat active hours require both start and end")
        if self.heartbeat_active_hours_start:
            _parse_clock(self.heartbeat_active_hours_start)
            if self.heartbeat_active_hours_end != "24:00":
                _parse_clock(self.heartbeat_active_hours_end)
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        return self

    @property
    def db_path(self) -> Path:
        expanded = self.database_path.replace("$HERMES_HOME", str(hermes_home()))
        return Path(os.path.expandvars(expanded)).expanduser().resolve()


def _parse_clock(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid time {value!r}; expected HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid time {value!r}; expected HH:MM")
    return hour, minute


def _parse_duration(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*", str(value or ""))
    if not match:
        raise ValueError(f"Invalid duration {value!r}; expected values such as 30m, 1h, or 1d")
    amount = float(match.group(1))
    if amount <= 0:
        raise ValueError("duration must be positive")
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[match.group(2)]
    seconds = amount * multiplier
    if seconds < 1 or seconds > 31 * 86400:
        raise ValueError("duration must be between 1 second and 31 days")
    return seconds


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read Hermes configuration") from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Hermes configuration could not be read: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Hermes configuration root must be a mapping")
    return value


def _plugin_section(document: dict[str, Any]) -> dict[str, Any]:
    plugins = document.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    direct = plugins.get("conscious-agency") or plugins.get("conscious_agency")
    if isinstance(direct, dict):
        return direct
    entries = plugins.get("entries")
    if isinstance(entries, dict):
        entry = entries.get("conscious_agency") or entries.get("conscious-agency")
        if isinstance(entry, dict):
            config = entry.get("config", entry)
            return config if isinstance(config, dict) else {}
    return {}


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> AgencyConfig:
    """Load plugin configuration and reject unknown keys as likely unsafe typos."""

    source = _plugin_section(_read_yaml(path or hermes_home() / "config.yaml"))
    # One-release migration bridge for 0.6.x installations. The native heartbeat replaces the
    # Hermes cron job; these keys are translated in memory so an upgrade can start safely before
    # Control Center or the operator rewrites config.yaml.
    legacy = dict(source)
    if "heartbeat_enabled" not in source and "allow_scheduled_reflection" in legacy:
        source["heartbeat_enabled"] = legacy["allow_scheduled_reflection"]
    if "heartbeat_every" not in source and "cron_schedule" in legacy:
        schedule = str(legacy["cron_schedule"] or "").strip().lower()
        source["heartbeat_every"] = schedule.removeprefix("every ") or "30m"
    if "heartbeat_target" not in source and "cron_delivery" in legacy:
        source["heartbeat_target"] = "none" if legacy["cron_delivery"] == "local" else "last"
    if "heartbeat_disable_thinking" not in source and "cron_disable_thinking" in legacy:
        source["heartbeat_disable_thinking"] = legacy["cron_disable_thinking"]
    if (
        "educational_allow_heartbeat_tools" not in source
        and "educational_allow_cron_tools" in legacy
    ):
        source["educational_allow_heartbeat_tools"] = legacy["educational_allow_cron_tools"]
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
        source.pop(retired, None)
    if overrides:
        source = {**source, **overrides}
    known = {item.name for item in fields(AgencyConfig)}
    unknown = sorted(set(source) - known)
    if unknown:
        raise ValueError("unknown Conscious Agency setting(s): " + ", ".join(unknown))
    return AgencyConfig(**source).validate()
