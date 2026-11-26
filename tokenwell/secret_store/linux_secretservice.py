"""Linux implementation of the secret-store interface, via the Secret
Service D-Bus API — through the `secret-tool` CLI (part of libsecret,
usually packaged as `libsecret-tools` / `libsecret`), the same "shell out to
a system CLI" shape macos_keychain.py uses for `security`. See
docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is the
ONLY file that should ever shell out to `secret-tool` or know its shape.
Callers use tokenwell.secret_store, never this module directly.

UNVERIFIED: written to `secret-tool`'s documented command shape but not yet
exercised against a live Secret Service provider (GNOME Keyring, KWallet's
secret-service compat, etc.). See CONTRIBUTING.md's "third-OS smoke test"
guidance for what closes that gap.

Unlike macOS, where the Keychain is always present, a Secret Service provider
is not guaranteed to be running: a headless install with no desktop session
may have neither a D-Bus session bus nor a provider. Every function here
fails loudly with an actionable message in that case rather than falling back
to writing a credential to disk in plain text.
"""

from __future__ import annotations

import shutil
import subprocess
import logging

SERVICE = "tokenwell.oauth"
LEGACY_SERVICE = "claude-unlimited.oauth"
_LOGGER = logging.getLogger(__name__)


class SecretStoreError(RuntimeError):
    pass


def _require_secret_tool() -> None:
    if shutil.which("secret-tool") is None:
        raise SecretStoreError(
            "`secret-tool` was not found on PATH. Tokenwell stores credentials "
            "via the Secret Service API (libsecret) on Linux — install it with, e.g., "
            "`sudo apt install libsecret-tools` (Debian/Ubuntu) or `sudo dnf install "
            "libsecret` (Fedora), and make sure a provider (GNOME Keyring, KWallet, or "
            "similar) is running in your session."
        )


def set_token(profile_id: str, token: str) -> None:
    _require_secret_tool()
    result = subprocess.run(
        ["secret-tool", "store", "--label", f"Tokenwell: {profile_id}",
         "service", SERVICE, "account", profile_id],
        input=token, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SecretStoreError(
            f"secret-tool store failed for profile {profile_id!r}: {(result.stderr or result.stdout).strip()}"
        )


def get_token(profile_id: str) -> str:
    _require_secret_tool()
    result = _lookup(SERVICE, profile_id)
    if result.returncode == 0 and result.stdout:
        return result.stdout.strip()
    old = _lookup(LEGACY_SERVICE, profile_id)
    if old.returncode != 0 or not old.stdout:
        raise SecretStoreError(f"No stored credential found for profile {profile_id!r}.")
    token = old.stdout.strip()
    try:
        set_token(profile_id, token)
    except Exception as exc:
        _LOGGER.warning("Tokenwell could not complete legacy secret migration for profile %s: %s",
                        profile_id, exc)
        return token
    try:
        if get_token_new(profile_id) != token:
            raise SecretStoreError("read-back mismatch")
    except Exception as exc:
        _clear_new_after_failed_migration(profile_id)
        _LOGGER.warning("Tokenwell could not complete legacy secret migration for profile %s: %s",
                        profile_id, exc)
        return token
    subprocess.run(["secret-tool", "clear", "service", LEGACY_SERVICE, "account", profile_id],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return token


def get_token_new(profile_id: str) -> str:
    """Read only the new entry; used to verify a legacy rewrite."""
    result = _lookup(SERVICE, profile_id)
    if result.returncode != 0 or not result.stdout:
        raise SecretStoreError("new secret entry missing after migration")
    # secret-tool lookup prints the secret with no trailing newline; strip
    # defensively, matching macos_keychain.get_token.
    return result.stdout.strip()


def _lookup(service: str, profile_id: str):
    return subprocess.run(["secret-tool", "lookup", "service", service, "account", profile_id],
                          capture_output=True, text=True)


def _clear_new_after_failed_migration(profile_id: str) -> None:
    command = ["secret-tool", "clear", "service", SERVICE, "account", profile_id]
    for _attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            remaining = _lookup(SERVICE, profile_id)
            if (result.returncode == 0 and
                    (remaining.returncode != 0 or not remaining.stdout)):
                return
        except Exception:
            pass
    _LOGGER.error("Tokenwell could not remove failed migrated secret for profile %s", profile_id)


def delete_token(profile_id: str) -> None:
    if shutil.which("secret-tool") is None:
        return  # nothing to delete if the tool (and so the store) isn't even present
    for service in (SERVICE, LEGACY_SERVICE):
        subprocess.run(["secret-tool", "clear", "service", service, "account", profile_id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def has_token(profile_id: str) -> bool:
    try:
        return bool(get_token(profile_id))
    except Exception:
        return False
