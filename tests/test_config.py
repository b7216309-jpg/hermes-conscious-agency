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
    with pytest.raises(ValueError, match="educational_allow_cron_tools"):
        AgencyConfig(educational_allow_cron_tools="yes").validate()


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
    assert load_config(path).cron_delivery == "telegram"


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
