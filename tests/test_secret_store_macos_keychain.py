"""set_token must never leave a window with no credential present.

`add-generic-password -U` updates in place, so no separate delete call is
needed. Deleting first would destroy the stored credential whenever the add
half then failed (locked Keychain, disk full, permission denial).
"""

import subprocess
from unittest.mock import MagicMock, patch

from tokenwell.secret_store import macos_keychain


def test_set_token_calls_add_with_update_flag_only_no_delete_first():
    with patch("tokenwell.secret_store.macos_keychain.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        macos_keychain.set_token("profile-1", "secret-token")

    assert mock_run.call_count == 1  # exactly one subprocess call — no delete first
    args = mock_run.call_args.args[0]
    assert args[:2] == ["security", "add-generic-password"]
    assert "-U" in args
    assert "delete-generic-password" not in args


def test_set_token_does_not_swallow_a_real_add_failure():
    with patch("tokenwell.secret_store.macos_keychain.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["security"])
        try:
            macos_keychain.set_token("profile-1", "secret-token")
            assert False, "expected CalledProcessError to propagate"
        except subprocess.CalledProcessError:
            pass

    # With no delete call, a failed add cannot destroy a pre-existing
    # credential; the single-call assertion above is what guarantees that.


def test_legacy_secret_is_rewritten_verified_and_old_entry_deleted():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "find-generic-password":
            service = command[command.index("-s") + 1]
            if service == macos_keychain._service("profile-1"):
                if len([c for c in calls if c[1] == "add-generic-password"]) == 0:
                    raise subprocess.CalledProcessError(44, command)
                return MagicMock(stdout="secret-token\n")
            return MagicMock(stdout="secret-token\n")
        return MagicMock(returncode=0)

    with patch.object(macos_keychain.subprocess, "run", side_effect=run):
        assert macos_keychain.get_token("profile-1") == "secret-token"

    assert [c for c in calls if c[1] == "delete-generic-password"] == [
        ["security", "delete-generic-password", "-a", "profile-1",
         "-s", macos_keychain._legacy_service("profile-1")]
    ]


def test_failed_verify_clears_new_entry_so_second_read_uses_legacy():
    state = {"new": False, "legacy": True}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "find-generic-password":
            service = command[command.index("-s") + 1]
            if service == macos_keychain._service("profile-1"):
                if not state["new"]:
                    raise subprocess.CalledProcessError(44, command)
                return MagicMock(stdout="wrong-token\n")
            if state["legacy"]:
                return MagicMock(stdout="legacy-token\n")
            raise subprocess.CalledProcessError(44, command)
        if command[1] == "add-generic-password":
            state["new"] = True
        elif command[1] == "delete-generic-password":
            service = command[command.index("-s") + 1]
            if service == macos_keychain._service("profile-1"):
                state["new"] = False
        return MagicMock(returncode=0)

    with patch.object(macos_keychain.subprocess, "run", side_effect=run):
        assert macos_keychain.get_token("profile-1") == "legacy-token"
        assert macos_keychain.get_token("profile-1") == "legacy-token"
    assert state["new"] is False
    assert sum(c[1] == "delete-generic-password" and c[-1] == macos_keychain._service("profile-1")
               for c in calls) == 2


def test_failed_write_keeps_the_old_entry():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "find-generic-password":
            service = command[command.index("-s") + 1]
            if service == macos_keychain._service("profile-1"):
                raise subprocess.CalledProcessError(44, command)
            return MagicMock(stdout="legacy-token\n")
        if command[1] == "add-generic-password":
            raise subprocess.CalledProcessError(1, command)
        return MagicMock(returncode=0)

    with patch.object(macos_keychain.subprocess, "run", side_effect=run):
        assert macos_keychain.get_token("profile-1") == "legacy-token"

    assert not any(c[1] == "delete-generic-password" for c in calls)


def test_failed_verify_retries_cleanup_and_keeps_old_entry():
    state = {"new": False, "legacy": True, "clears": 0}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "find-generic-password":
            service = command[command.index("-s") + 1]
            if service == macos_keychain._service("profile-1"):
                if not state["new"]:
                    raise subprocess.CalledProcessError(44, command)
                return MagicMock(stdout="wrong-token\n")
            return MagicMock(stdout="legacy-token\n")
        if command[1] == "add-generic-password":
            state["new"] = True
        elif command[1] == "delete-generic-password":
            state["clears"] += 1
            if state["clears"] == 2:
                state["new"] = False
        return MagicMock(returncode=0)

    with patch.object(macos_keychain.subprocess, "run", side_effect=run):
        assert macos_keychain.get_token("profile-1") == "legacy-token"

    assert state["clears"] == 2
    assert state["new"] is False


def test_cleanup_failure_is_retried_and_logged():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "delete-generic-password":
            return MagicMock(returncode=1)
        return MagicMock(stdout="still-present\n")

    with patch.object(macos_keychain.subprocess, "run", side_effect=run), \
         patch.object(macos_keychain, "_get_service_token", return_value="still-present"), \
         patch.object(macos_keychain._LOGGER, "error") as error:
        macos_keychain._clear_new_after_failed_migration("profile-1")

    assert sum(c[1] == "delete-generic-password" for c in calls) == 2
    error.assert_called_once()
    assert error.call_args.args[1] == "profile-1"


def test_delete_token_clears_new_and_legacy_service_names():
    with patch.object(macos_keychain.subprocess, "run") as mock_run:
        macos_keychain.delete_token("profile-1")

    assert [call.args[0][-1] for call in mock_run.call_args_list] == [
        macos_keychain._service("profile-1"),
        macos_keychain._legacy_service("profile-1"),
    ]
