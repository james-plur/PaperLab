"""macOS LaunchAgent installation for weekly incremental updates."""
from __future__ import annotations

import plistlib
import shlex
import subprocess
from pathlib import Path

from .config import Config


LABEL = "com.paperlab_local.weekly"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def build_plist(cfg: Config, config_path: str) -> dict:
    executable = cfg.root / ".venv/bin/paperlab_local"
    logs = cfg.pdf_root / "logs"
    env_file = cfg.root / ".env"
    command = (
        f"if [ -f {shlex.quote(str(env_file))} ]; then source {shlex.quote(str(env_file))}; fi; "
        f"exec {shlex.quote(str(executable))} --config "
        f"{shlex.quote(str(Path(config_path).resolve()))} update"
    )
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "WorkingDirectory": str(cfg.root),
        "StartCalendarInterval": {
            "Weekday": cfg.schedule.weekday,
            "Hour": cfg.schedule.hour,
            "Minute": cfg.schedule.minute,
        },
        "RunAtLoad": False,
        "StandardOutPath": str(logs / "update.log"),
        "StandardErrorPath": str(logs / "update.error.log"),
        "EnvironmentVariables": {
            "PATH": f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin",
        },
    }


def install(cfg: Config, config_path: str) -> Path:
    destination = plist_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    (cfg.pdf_root / "logs").mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plistlib.dumps(build_plist(cfg, config_path), sort_keys=False))
    domain = f"gui/{Path.home().stat().st_uid}"
    subprocess.run(["launchctl", "bootout", domain, str(destination)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(destination)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")
    return destination


def remove() -> bool:
    destination = plist_path()
    if not destination.exists():
        return False
    domain = f"gui/{Path.home().stat().st_uid}"
    subprocess.run(["launchctl", "bootout", domain, str(destination)], capture_output=True)
    destination.unlink()
    return True


def status() -> dict:
    destination = plist_path()
    result = subprocess.run(["launchctl", "print", f"gui/{Path.home().stat().st_uid}/{LABEL}"],
                            capture_output=True, text=True)
    return {"installed": destination.exists(), "loaded": result.returncode == 0,
            "path": str(destination)}
