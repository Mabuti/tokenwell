"""macOS implementation of the secret-store interface, via the `security` CLI.

See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is the
ONLY file that should ever shell out to `security` or know Keychain's shape.
Callers use tokenwell.secret_store, never this module directly.
"""

from __future__ import annotations

import subprocess
import logging

SERVICE_PREFIX = "tokenwell.oauth"
LEGACY_SERVICE_PREFIX = "claude-unlimited.oauth"
_LOGGER = logging.getLogger(__name__)


def _service(profile_id: str) -> str:
    return f"{SERVICE_PREFIX}.{profile_id}"


def _legacy_service(profile_id: str) -> str:
    return f"{LEGACY_SERVICE_PREFIX}.{profile_id}"


def set_token(profile_id: str, token: str) -> None:
    # `-U` updates the item in place if it already exists, so no delete step
    # is needed. Deleting first would risk losing the credential entirely if
    # the add then failed (locked Keychain, disk full, permission denial).
    service = _service(profile_id)
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", profile_id, "-s", service, "-w", token],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def get_token(profile_id: str) -> str:
    try:
        return _get_service_token(_service(profile_id))
    except subprocess.CalledProcessError:
        token = _get_service_token(_legacy_service(profile_id))
        try:
            set_token(profile_id, token)
        except Exception as exc:
            _LOGGER.warning("Tokenwell could not complete legacy secret migration for profile %s: %s",
                            profile_id, exc)
            return token
        try:
            if _get_service_token(_service(profile_id)) != token:
                raise RuntimeError("read-back mismatch")
        except Exception as exc:
            _clear_new_after_failed_migration(profile_id)
            _LOGGER.warning("Tokenwell could not complete legacy secret migration for profile %s: %s",
                            profile_id, exc)
            return token
        subprocess.run(["security", "delete-generic-password", "-a", profile_id,
                        "-s", _legacy_service(profile_id)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return token


def _get_service_token(service: str) -> str:
    cp = subprocess.run(["security", "find-generic-password", "-a", service.split(".")[-1],
                         "-s", service, "-w"], check=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
    return cp.stdout.strip()


def _clear_new_after_failed_migration(profile_id: str) -> None:
    service = _service(profile_id)
    command = ["security", "delete-generic-password", "-a", profile_id, "-s", service]
    for _attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            try:
                _get_service_token(service)
            except subprocess.CalledProcessError:
                if result.returncode == 0:
                    return
        except Exception:
            pass
    _LOGGER.error("Tokenwell could not remove failed migrated secret for profile %s", profile_id)


def delete_token(profile_id: str) -> None:
    for service in (_service(profile_id), _legacy_service(profile_id)):
        subprocess.run(["security", "delete-generic-password", "-a", profile_id, "-s", service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def has_token(profile_id: str) -> bool:
    try:
        return bool(get_token(profile_id))
    except Exception:
        return False
