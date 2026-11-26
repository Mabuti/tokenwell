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
import logging
from pathlib import Path
from ..config import APP_DIR

LABEL = "com.tokenwell.daemon"
LEGACY_LABEL = "com.claude-unlimited.daemon"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
LEGACY_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LEGACY_LABEL}.plist"
LOG_DIR = APP_DIR / "logs"
_LOGGER = logging.getLogger(__name__)


class DaemonInstallerError(RuntimeError):
    pass


def _legacy_data_was_migrated() -> bool:
    home = Path.home()
    legacy = home / ".claude-unlimited"
    new = home / ".tokenwell"
    if not (new / "tokenwell.db").is_file():
        return False
    if legacy.is_symlink():
        try:
            if legacy.resolve() == new.resolve():
                return True
        except OSError:
            pass
    return not (legacy / "claude_unlimited.db").is_file()


def _leave_legacy_stopped() -> None:
    print(
        "Legacy data has been migrated; the legacy service was left stopped. "
        "Start Tokenwell with `tokenwell start`.",
        flush=True,
    )


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def rewrite_service_paths_after_migration() -> None:
    """Rewrite launchd log paths after the daemon migrates its data."""
    old_root = str(Path.home() / ".claude-unlimited")
    new_root = str(Path.home() / ".tokenwell")
    try:
        if not PLIST_PATH.is_file():
            return
        plist = plistlib.loads(PLIST_PATH.read_bytes())
        changed = False
        for key in ("StandardOutPath", "StandardErrorPath"):
            value = plist.get(key)
            if isinstance(value, str) and old_root in value:
                plist[key] = value.replace(old_root, new_root)
                changed = True
        if changed:
            PLIST_PATH.write_bytes(plistlib.dumps(plist))
    except Exception:
        _LOGGER.exception("Tokenwell could not update launchd log paths after migration")


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

    new_present = PLIST_PATH.exists()
    new_contents = PLIST_PATH.read_bytes() if new_present else None
    new_loaded = new_present and _run_launchctl(
        "print", f"{_domain_target()}/{LABEL}"
    ).returncode == 0

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
    except (OSError, DaemonInstallerError) as exc:
        rollback_errors = [str(exc)]
        try:
            removed = _run_launchctl("bootout", f"{_domain_target()}/{LABEL}")
            if removed.returncode != 0:
                raise DaemonInstallerError(
                    f"launchd Tokenwell bootout rollback failed: "
                    f"{(removed.stderr or removed.stdout).strip()}")
        except (OSError, subprocess.SubprocessError) as rollback_exc:
            rollback_errors.append(f"launchd Tokenwell bootout rollback failed: {rollback_exc}")
        except DaemonInstallerError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            if new_contents is None:
                PLIST_PATH.unlink(missing_ok=True)
            else:
                PLIST_PATH.write_bytes(new_contents)
                if new_loaded:
                    restored = _run_launchctl("bootstrap", _domain_target(), str(PLIST_PATH))
                    if restored.returncode != 0:
                        raise DaemonInstallerError(
                            f"launchd Tokenwell rollback failed: "
                            f"{(restored.stderr or restored.stdout).strip()}")
        except (OSError, DaemonInstallerError) as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if old_running:
            if _legacy_data_was_migrated():
                _leave_legacy_stopped()
            else:
                try:
                    restored = _run_launchctl("bootstrap", _domain_target(), str(LEGACY_PLIST_PATH))
                    if restored.returncode != 0:
                        raise DaemonInstallerError(
                            f"launchctl bootstrap rollback failed: "
                            f"{(restored.stderr or restored.stdout).strip()}")
                except (OSError, DaemonInstallerError) as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
        raise DaemonInstallerError("; ".join(error for error in rollback_errors if error)) from exc

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
