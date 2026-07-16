from __future__ import annotations

import textwrap

import pytest

from agency.config import AgencyConfig, load_config


def test_loads_current_hermes_plugin_shape(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent("""
        plugins:
          conscious-agency:
            allow_proactive_messages: true
            daily_message_limit: 3
    """),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.allow_proactive_messages is True
    assert config.daily_message_limit == 3
    assert config.educational_bypass_proactive_gates is False


def test_educational_controls_are_strict_booleans():
    with pytest.raises(ValueError, match="educational_allow_heartbeat_tools"):
        AgencyConfig(educational_allow_heartbeat_tools="yes").validate()


def test_subjective_mode_is_a_strict_research_condition():
    assert AgencyConfig(educational_subjective_mode="continuity").validate()
    with pytest.raises(ValueError, match="educational_subjective_mode"):
        AgencyConfig(educational_subjective_mode="alive").validate()


def test_rejects_unknown_setting_as_likely_typo(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugins:\n  conscious-agency:\n    allow_proactive_message: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allow_proactive_message"):
        load_config(path)


def test_loads_legacy_entries_shape(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent("""
        plugins:
          entries:
            conscious_agency:
              config:
                cron_delivery: telegram
    """),
        encoding="utf-8",
    )
    migrated = load_config(path)
    assert migrated.heartbeat_target == "last"


def test_migrates_retired_cron_configuration(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugins:\n  conscious-agency:\n"
        "    allow_scheduled_reflection: true\n"
        "    cron_schedule: every 1h\n"
        "    cron_delivery: local\n"
        "    cron_disable_thinking: true\n"
        "    educational_allow_cron_tools: true\n",
        encoding="utf-8",
    )
    migrated = load_config(path)
    assert migrated.heartbeat_enabled is True
    assert migrated.heartbeat_every == "1h"
    assert migrated.heartbeat_target == "none"
    assert migrated.heartbeat_disable_thinking is True
    assert migrated.educational_allow_heartbeat_tools is True


def test_validates_heartbeat_schedule_and_active_hours():
    AgencyConfig(
        heartbeat_every="45m",
        heartbeat_active_hours_start="08:00",
        heartbeat_active_hours_end="22:00",
    ).validate()
    with pytest.raises(ValueError, match="Invalid duration"):
        AgencyConfig(heartbeat_every="often").validate()
    with pytest.raises(ValueError, match="require both"):
        AgencyConfig(heartbeat_active_hours_start="08:00").validate()
    with pytest.raises(ValueError, match="heartbeat_max_iterations"):
        AgencyConfig(heartbeat_max_iterations=0).validate()


@pytest.mark.parametrize("value", ["25:00", "12:60", "bad", ""])
def test_rejects_invalid_quiet_time(value):
    with pytest.raises(ValueError):
        AgencyConfig(quiet_hours_start=value).validate()


def test_rejects_unsafe_ranges():
    with pytest.raises(ValueError):
        AgencyConfig(daily_message_limit=-1).validate()
    with pytest.raises(ValueError):
        AgencyConfig(maximum_message_chars=50).validate()


def test_rejects_string_booleans_and_malformed_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugins:\n  conscious-agency:\n    allow_proactive_messages: 'false'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a boolean"):
        load_config(path)

    path.write_text("plugins: [unterminated", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read"):
        load_config(path)


def test_rejects_invalid_environment_variable_name():
    with pytest.raises(ValueError, match="environment variable"):
        AgencyConfig(database_key_env="BAD-NAME").validate()
