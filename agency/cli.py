"""Operator-only CLI and explicit-user slash command."""

from __future__ import annotations

import argparse
import json
import shlex
from typing import Any

from .config import load_config
from .cron import cron_action, install_cron
from .engine import AgencyEngine
from .store import AgencyStore


def _engine() -> AgencyEngine:
    config = load_config()
    return AgencyEngine(AgencyStore(config), config)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="agency_action")
    subs.add_parser("status", help="Show safety, focus, and proactive status")
    subs.add_parser("snapshot", help="Show the complete persistent agency snapshot")
    events = subs.add_parser("events", help="Show recent diagnostic ledger events")
    events.add_argument("--limit", type=int, default=25)
    journal = subs.add_parser(
        "subjective-journal",
        help="Export longitudinal subjective entries as JSON",
    )
    journal.add_argument("--limit", type=int, default=100)
    journal.add_argument("--model", default="")
    journal.add_argument("--source", choices=["cron", "conversation"], default="")
    pause = subs.add_parser("pause", help="Pause agency behavior")
    pause.add_argument("reason", nargs="*", default=[])
    subs.add_parser("resume", help="Resume agency behavior (operator-only)")
    focus = subs.add_parser("focus", help="Set or clear global focus")
    focus.add_argument("value", nargs="*", default=[])
    focus.add_argument("--reason", default="")
    intentions = subs.add_parser("intentions", help="List intentions")
    intentions.add_argument("--status", default="active")
    add = subs.add_parser("add-intention", help="Create an explicit intention")
    add.add_argument("title")
    add.add_argument("--rationale", default="")
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--autonomy", choices=["reflect", "propose", "message"], default="propose")
    add.add_argument("--due-at", default=None, help="Optional ISO-8601 deadline")
    update = subs.add_parser("update-intention", help="Update intention status or deadline")
    update.add_argument("id")
    update.add_argument("--status", choices=["active", "blocked", "completed", "cancelled"])
    due_group = update.add_mutually_exclusive_group()
    due_group.add_argument("--due-at", default=None, help="Set an ISO-8601 deadline")
    due_group.add_argument("--clear-due", action="store_true", help="Clear the current deadline")
    tick = subs.add_parser("tick", help="Evaluate hard proactive gates without sending")
    tick.add_argument("--record-silent", action="store_true")
    subs.add_parser("install-cron", help="Install the bounded proactive cron job")
    for action in ("pause-cron", "resume-cron", "run-cron", "remove-cron"):
        subs.add_parser(action)
    parser.set_defaults(func=cli_command)


def cli_command(args: argparse.Namespace) -> int:
    action = getattr(args, "agency_action", None)
    if not action:
        print("Usage: hermes conscious-agency <action>")
        print(
            "Actions: status, snapshot, events, subjective-journal, pause, resume, focus, "
            "intentions, add-intention, update-intention, tick, install-cron"
        )
        return 2
    try:
        engine = _engine()
        if action == "status":
            _print(
                {
                    "database_path": str(engine.store.path),
                    "runtime": engine.runtime(),
                    "focus": engine.workspace().get("focus"),
                    "gates": engine.evaluate_tick(),
                    "subjective": engine.snapshot()["subjective"],
                }
            )
        elif action == "snapshot":
            _print(engine.snapshot())
        elif action == "events":
            _print(engine.store.recent_events(args.limit))
        elif action == "subjective-journal":
            _print(
                engine.store.recent_subjective_entries(
                    args.limit,
                    model_id=args.model,
                    source=args.source,
                )
            )
        elif action == "pause":
            _print(engine.pause(" ".join(args.reason) or "Paused by operator"))
        elif action == "resume":
            _print(engine.resume_by_user())
        elif action == "focus":
            _print(engine.set_focus(" ".join(args.value), args.reason))
        elif action == "intentions":
            _print(engine.store.list_intentions(args.status, 100))
        elif action == "add-intention":
            _print(
                engine.store.add_intention(
                    args.title,
                    rationale=args.rationale,
                    priority=args.priority,
                    autonomy=args.autonomy,
                    due_at=args.due_at,
                    source="operator",
                )
            )
        elif action == "update-intention":
            due_at = "" if args.clear_due else args.due_at
            _print(engine.store.update_intention(args.id, status=args.status, due_at=due_at))
        elif action == "tick":
            gates = engine.evaluate_tick()
            _print(gates)
            if args.record_silent:
                engine.record_decision("silent", "Manual operator gate inspection")
        elif action == "install-cron":
            _print(install_cron())
        elif action.endswith("-cron"):
            verb = action.removesuffix("-cron")
            print(cron_action(verb))
        return 0
    except Exception as exc:
        print(f"conscious-agency: {type(exc).__name__}: {exc}")
        return 1


SLASH_HELP = """/agency commands:
  status
  intentions
  focus <text>
  pause [reason]
  resume
  tick
"""


def slash_command(raw_args: str) -> str:
    try:
        argv = shlex.split(raw_args)
    except ValueError as exc:
        return f"Invalid arguments: {exc}"
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return SLASH_HELP
    engine = _engine()
    action, rest = argv[0], argv[1:]
    if action == "status":
        return json.dumps(
            {
                "database_path": str(engine.store.path),
                "runtime": engine.runtime(),
                "focus": engine.workspace().get("focus"),
                "gates": engine.evaluate_tick(),
                "subjective": engine.snapshot()["subjective"],
            },
            indent=2,
            ensure_ascii=False,
        )
    if action == "intentions":
        return json.dumps(engine.store.list_intentions("active", 20), indent=2, ensure_ascii=False)
    if action == "focus":
        return json.dumps(engine.set_focus(" ".join(rest)), indent=2, ensure_ascii=False)
    if action == "pause":
        return json.dumps(
            engine.pause(" ".join(rest) or "Paused by user"), indent=2, ensure_ascii=False
        )
    if action == "resume":
        return json.dumps(engine.resume_by_user(), indent=2, ensure_ascii=False)
    if action == "tick":
        return json.dumps(engine.evaluate_tick(), indent=2, ensure_ascii=False)
    return f"Unknown agency command: {action}\n\n{SLASH_HELP}"
