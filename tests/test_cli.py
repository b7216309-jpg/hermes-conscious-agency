from __future__ import annotations

import argparse

from agency.cli import register_cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_cli(parser)
    return parser


def test_intention_cli_accepts_deadlines_and_explicit_clear():
    parser = _parser()

    added = parser.parse_args(
        ["add-intention", "Ship release", "--due-at", "2026-07-16T08:00:00+02:00"]
    )
    updated = parser.parse_args(["update-intention", "abc", "--due-at", "2026-07-17"])
    cleared = parser.parse_args(["update-intention", "abc", "--clear-due"])

    assert added.due_at == "2026-07-16T08:00:00+02:00"
    assert updated.due_at == "2026-07-17"
    assert updated.clear_due is False
    assert cleared.due_at is None
    assert cleared.clear_due is True


def test_intention_cli_rejects_set_and_clear_together():
    parser = _parser()

    try:
        parser.parse_args(["update-intention", "abc", "--due-at", "2026-07-17", "--clear-due"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("mutually exclusive deadline options were accepted")


def test_subjective_journal_cli_filters_model_and_source():
    args = _parser().parse_args(
        ["subjective-journal", "--limit", "500", "--model", "model-a", "--source", "cron"]
    )
    assert args.limit == 500
    assert args.model == "model-a"
    assert args.source == "cron"
