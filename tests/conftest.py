from __future__ import annotations

from dataclasses import replace

import pytest

from agency.config import AgencyConfig


@pytest.fixture
def config(tmp_path):
    return AgencyConfig(
        database_path=str(tmp_path / "agency.db"),
        quiet_hours_start="00:00",
        quiet_hours_end="00:00",
        allow_proactive_messages=True,
        daily_message_limit=2,
        cooldown_hours=6,
        minimum_user_silence_hours=4,
    ).validate()


@pytest.fixture
def config_factory(config):
    def factory(**updates):
        return replace(config, **updates).validate()

    return factory
