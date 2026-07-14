"""Configuration loading and validation.

The plugin reads ``plugins.conscious-agency`` from Hermes' config.yaml.  A
legacy ``plugins.entries.conscious_agency.config`` shape is accepted to make
the package tolerant of older or experimental Hermes builds.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    allow_scheduled_reflection: bool = True
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
    cron_schedule: str = "every 3h"
    cron_delivery: str = "local"
    cron_name: str = "Hermes Conscious Agency Tick"
    manual_run_timeout_seconds: int = 660

    def validate(self) -> AgencyConfig:
        boolean_fields = (
            "enabled",
            "inject_context",
            "database_encryption",
            "allow_scheduled_reflection",
            "allow_proactive_messages",
            "require_prior_user_interaction",
            "store_transcript_excerpts",
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
            "manual_run_timeout_seconds",
        )
        string_fields = (
            "database_path",
            "database_key_env",
            "timezone",
            "quiet_hours_start",
            "quiet_hours_end",
            "cron_schedule",
            "cron_delivery",
            "cron_name",
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
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.database_key_env):
            raise ValueError("database_key_env must be a valid environment variable name")
        if len(self.cron_schedule) > 200 or len(self.cron_delivery) > 500:
            raise ValueError("cron_schedule or cron_delivery is too long")
        if len(self.cron_name) > 200:
            raise ValueError("cron_name is too long")
        if self.daily_message_limit < 0:
            raise ValueError("daily_message_limit must be non-negative")
        if self.cooldown_hours < 0 or self.minimum_user_silence_hours < 0:
            raise ValueError("cooldown and silence periods must be non-negative")
        if not 80 <= self.maximum_message_chars <= 4000:
            raise ValueError("maximum_message_chars must be between 80 and 4000")
        if not 500 <= self.context_char_limit <= 12000:
            raise ValueError("context_char_limit must be between 500 and 12000")
        if not 0 <= self.excerpt_char_limit <= 4000:
            raise ValueError("excerpt_char_limit must be between 0 and 4000")
        if self.event_retention_days < 1 or self.maximum_events < 100:
            raise ValueError("event retention must keep at least 1 day and 100 events")
        if not 0 <= self.maximum_reflections_per_tick <= 5:
            raise ValueError("maximum_reflections_per_tick must be between 0 and 5")
        if not 0 <= self.maximum_state_changes_per_tick <= 10:
            raise ValueError("maximum_state_changes_per_tick must be between 0 and 10")
        if not 30 <= self.manual_run_timeout_seconds <= 3600:
            raise ValueError("manual_run_timeout_seconds must be between 30 and 3600")
        _parse_clock(self.quiet_hours_start)
        _parse_clock(self.quiet_hours_end)
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
    if overrides:
        source = {**source, **overrides}
    known = {item.name for item in fields(AgencyConfig)}
    unknown = sorted(set(source) - known)
    if unknown:
        raise ValueError("unknown Conscious Agency setting(s): " + ", ".join(unknown))
    return AgencyConfig(**source).validate()
