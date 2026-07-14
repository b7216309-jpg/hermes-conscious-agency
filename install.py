from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _hermes_executable() -> str | None:
    executable = shutil.which("hermes")
    if executable:
        return executable
    user_executable = Path.home() / ".local" / "bin" / "hermes"
    return str(user_executable) if user_executable.is_file() else None


def _hermes_home(value: str) -> Path:
    raw = value or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or update Hermes Conscious Agency.")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-enable", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    home = _hermes_home(args.hermes_home)
    plugins_dir = home / "plugins"
    destination = plugins_dir / "conscious-agency"
    if not (source / "__init__.py").is_file() or not (source / "plugin.yaml").is_file():
        print(f"Invalid source tree: {source}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"Would install {source} -> {destination}")
        return 0
    plugins_dir.mkdir(parents=True, exist_ok=True)
    stage = plugins_dir / ".conscious-agency.installing"
    backup = plugins_dir / ".conscious-agency.backup"
    if backup.exists() and not destination.exists():
        os.replace(backup, destination)
    elif backup.exists():
        shutil.rmtree(backup)
    if stage.exists():
        shutil.rmtree(stage)
    ignore = shutil.ignore_patterns(
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "*.pyc",
        "tests",
        "build",
        "dist",
        "*.egg-info",
        ".env",
        "*.db*",
    )
    shutil.copytree(source, stage, ignore=ignore)
    try:
        if destination.exists():
            os.replace(destination, backup)
        os.replace(stage, destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if backup.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
    print(f"Installed conscious-agency to {destination}")
    if not args.no_enable:
        hermes = _hermes_executable()
        if hermes:
            completed = subprocess.run(
                [hermes, "plugins", "enable", "conscious-agency", "--no-allow-tool-override"],
                check=False,
            )
            if completed.returncode:
                print(
                    "Plugin copied but Hermes could not enable it automatically.", file=sys.stderr
                )
                return completed.returncode
        else:
            print("Next: hermes plugins enable conscious-agency --no-allow-tool-override")
    print("Restart the Hermes gateway, then run: hermes conscious-agency status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
