from __future__ import annotations

import subprocess
import sys

import install


def _completed(args, returncode=0):
    return subprocess.CompletedProcess(args, returncode)


def test_fresh_install_enables_without_tool_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(install, "_hermes_executable", lambda: "/fake/hermes")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or _completed(args),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["install.py", "--hermes-home", str(tmp_path / ".hermes")],
    )

    assert install.main() == 0
    assert calls[0][0] == [
        "/fake/hermes",
        "plugins",
        "enable",
        "conscious-agency",
        "--no-allow-tool-override",
    ]
    assert calls[1][0] == [
        "/fake/hermes",
        "conscious-agency",
        "migrate-heartbeat",
    ]


def test_update_preserves_enablement_but_still_runs_migration(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins" / "conscious-agency").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(install, "_hermes_executable", lambda: "/fake/hermes")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or _completed(args),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["install.py", "--hermes-home", str(home)],
    )

    assert install.main() == 0
    assert [item[0] for item in calls] == [
        ["/fake/hermes", "conscious-agency", "migrate-heartbeat"]
    ]
