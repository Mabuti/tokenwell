"""macOS implementation of the daemon-installer interface, via launchd.

See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is
the ONLY file that should ever shell out to `launchctl` or know a launchd
plist's shape. Callers use tokenwell.daemon_installer, never this
module directly.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from ..config import APP_DIR

LABEL = "com.tokenwell.daemon"
LEGACY_LABEL = "com.claude-unlimited.daemon"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
LEGACY_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LEGACY_LABEL}.plist"
LOG_DIR = APP_DIR / "logs"


class DaemonInstallerError(RuntimeError):
    pass


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(port: int) -> None:
    """Writes the LaunchAgent plist and registers it to auto-start on login.
    Safe to call again to pick up a changed port — bootout-then-bootstrap
    is idempotent, unlike bootstrap alone (which errors if already loaded)."""

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    old_present = LEGACY_PLIST_PATH.exists()
    old_running = False
    if old_present:
        old_running = _run_launchctl("print", f"{_domain_target()}/{LEGACY_LABEL}").returncode == 0
        if old_running:
            stopped = _run_launchctl("bootout", f"{_domain_target()}/{LEGACY_LABEL}")
            if stopped.returncode != 0:
                raise DaemonInstallerError(
                    f"launchctl bootout failed: {(stopped.stderr or stopped.stdout).strip()}")

    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "tokenwell", "start", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        # Without this, stdout is block-buffered and daemon.out.log lags a
        # whole run — the log is only useful if it is written as it happens.
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(LOG_DIR / "daemon.out.log"),
        "StandardErrorPath": str(LOG_DIR / "daemon.err.log"),
    }
    try:
        with open(PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)

        _run_launchctl("bootout", f"{_domain_target()}/{LABEL}")  # ignore failure: fine if it wasn't loaded
        result = _run_launchctl("bootstrap", _domain_target(), str(PLIST_PATH))
        if result.returncode != 0:
            raise DaemonInstallerError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
    except (OSError, DaemonInstallerError):
        _run_launchctl("bootout", f"{_domain_target()}/{LABEL}")
        PLIST_PATH.unlink(missing_ok=True)
        if old_running:
            restored = _run_launchctl("bootstrap", _domain_target(), str(LEGACY_PLIST_PATH))
            if restored.returncode != 0:
                raise DaemonInstallerError(
                    f"launchctl bootstrap rollback failed: {(restored.stderr or restored.stdout).strip()}")
        raise

    _run_launchctl("bootout", f"{_domain_target()}/{LEGACY_LABEL}")
    LEGACY_PLIST_PATH.unlink(missing_ok=True)


def uninstall() -> None:
    for label in (LABEL, LEGACY_LABEL):
        _run_launchctl("bootout", f"{_domain_target()}/{label}")  # ignore failure: fine if it wasn't loaded
    PLIST_PATH.unlink(missing_ok=True)
    LEGACY_PLIST_PATH.unlink(missing_ok=True)


def is_installed() -> bool:
    return PLIST_PATH.exists()


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}

    result = _run_launchctl("print", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        return {"installed": True, "running": False, "pid": None}

    pid = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
            break
    return {"installed": True, "running": pid is not None, "pid": pid}


def start() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — run `tokenwell install` first.")
    result = _run_launchctl("kickstart", "-k", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        raise DaemonInstallerError(f"launchctl kickstart failed: {(result.stderr or result.stdout).strip()}")


def stop() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — nothing to stop.")
    result = _run_launchctl("kill", "SIGTERM", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        raise DaemonInstallerError(f"launchctl kill failed: {(result.stderr or result.stdout).strip()}")
