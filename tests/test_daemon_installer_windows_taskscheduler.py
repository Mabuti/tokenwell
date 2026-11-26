"""Command-shape and pidfile-logic tests only.

These lock in which schtasks commands get run; they do not exercise a real
Task Scheduler. See windows_taskscheduler.py's module docstring.
"""

from unittest.mock import MagicMock, patch

import pytest

from tokenwell.daemon_installer import windows_taskscheduler as backend


def _ok(stdout="", returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def isolated_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(backend, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def test_is_installed_reflects_schtasks_query_exit_code():
    with patch.object(backend, "_run", return_value=_ok(returncode=1)):
        assert backend.is_installed() is False
    with patch.object(backend, "_run", return_value=_ok(returncode=0)):
        assert backend.is_installed() is True


def test_install_creates_task_and_starts_it():
    calls = []

    def fake_run(*args):
        calls.append(args)
        return _ok()

    with patch.object(backend, "_run", side_effect=fake_run):
        backend.install(4317)

    assert ("schtasks", "/delete", "/tn", backend.LEGACY_TASK_NAME, "/f") in calls
    create_call = next(call for call in calls if len(call) > 1 and call[1] == "/create")
    assert create_call[:3] == ("schtasks", "/create", "/tn")
    assert backend.TASK_NAME in create_call
    # /tr points at a generated wrapper script rather than embedding the
    # command line directly: schtasks' /tr has no redirection syntax of its
    # own, Task Scheduler hands the process no std handles otherwise (see
    # install()'s comment), and a wrapper file sidesteps nesting quotes
    # inside schtasks' own parsing of /tr.
    tr_value = create_call[create_call.index("/tr") + 1]
    wrapper = backend.Path(tr_value.strip('"'))
    assert wrapper.name == "tokenwell_launcher.cmd"
    content = wrapper.read_text(encoding="ascii")
    assert "tokenwell start --port 4317" in content
    assert " -u " in content  # unbuffered — see the buffering half of the same bug
    assert ">>" in content and "daemon.out.log" in content and "daemon.err.log" in content
    assert ("schtasks", "/run", "/tn", backend.TASK_NAME) in calls


def test_start_delegates_to_a_detached_restart():
    """The process calling start() may be the daemon itself; the restart must
    outlive it, so start() spawns a detached helper rather than killing itself
    inline."""
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_detached_restart") as mock_restart:
        backend.start()
    mock_restart.assert_called_once()


def test_detached_restart_spawns_a_surviving_process():
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend.subprocess, "Popen") as mock_popen:
        backend._detached_restart()
    mock_popen.assert_called_once()
    # The helper script must reference both the kill and the task start.
    script = mock_popen.call_args[0][0][-1]
    assert "taskkill" in script and "schtasks" in script and backend.TASK_NAME in script


def test_install_raises_on_create_failure():
    with patch.object(backend, "_run", return_value=_ok(returncode=1, stdout="access denied")):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)


def test_failed_reinstall_restores_existing_task_definition_and_launcher():
    old_launcher = backend.LOG_DIR.parent / "tokenwell_launcher.cmd"
    old_launcher.write_text("previous launcher", encoding="ascii")
    definition = "<Task><Settings><Enabled>false</Enabled></Settings></Task>"
    calls = []

    def run(*args):
        calls.append(args)
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(returncode=1)
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME, "/xml"):
            return _ok(stdout=definition)
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)

    assert old_launcher.read_text(encoding="ascii") == "previous launcher"
    assert any(call[:4] == ("schtasks", "/create", "/tn", backend.TASK_NAME)
               and "/xml" in call for call in calls)


def test_failed_reinstall_restores_utf16_task_definition_byte_for_byte():
    definition = "<Task><Settings><Enabled>false</Enabled></Settings></Task>".encode("utf-16")
    restored = []

    def run(*args):
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(returncode=1)
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME, "/xml"):
            return _ok(stdout=definition)
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        if args[:5] == ("schtasks", "/create", "/tn", backend.TASK_NAME, "/xml"):
            restored.append(backend.Path(args[5]).read_bytes())
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)

    assert restored == [definition]


def test_failed_reinstall_attempts_legacy_restore_when_new_definition_restore_fails():
    calls = []

    def run(*args):
        calls.append(args)
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME, "/xml"):
            return _ok(stdout=b"\xff\xfe<Task />")
        if args == ("schtasks", "/query", "/tn", backend.TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        if args[:5] == ("schtasks", "/create", "/tn", backend.TASK_NAME, "/xml"):
            return _ok(returncode=1, stdout="restore failed")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError, match="restore failed"):
            backend.install(4317)

    assert ("schtasks", "/run", "/tn", backend.LEGACY_TASK_NAME) in calls


def test_failed_upgrade_does_not_restart_legacy_after_data_migration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    new = home / ".tokenwell"
    new.mkdir()
    (new / "tokenwell.db").write_bytes(b"db")
    (home / ".claude-unlimited").symlink_to(new, target_is_directory=True)
    monkeypatch.setattr(backend.Path, "home", classmethod(lambda cls: home))
    calls = []

    def run(*args):
        calls.append(args)
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)

    assert ("schtasks", "/run", "/tn", backend.LEGACY_TASK_NAME) not in calls


def test_upgrade_starts_new_task_before_deleting_old_task(tmp_path):
    calls = []

    def run(*args):
        calls.append(args)
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        backend.install(4317)

    assert ("schtasks", "/end", "/tn", backend.LEGACY_TASK_NAME) in calls
    start_new = calls.index(("schtasks", "/run", "/tn", backend.TASK_NAME))
    delete_old = calls.index(("schtasks", "/delete", "/tn", backend.LEGACY_TASK_NAME, "/f"))
    assert start_new < delete_old


def test_upgrade_removes_legacy_launcher_only_after_successful_task_delete():
    legacy_launcher = backend.LOG_DIR.parent / "daemon_launcher.cmd"
    legacy_launcher.write_text("legacy launcher", encoding="ascii")

    def run(*args):
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        backend.install(4317)

    assert not legacy_launcher.exists()


def test_failed_upgrade_leaves_legacy_launcher_runnable():
    legacy_launcher = backend.LOG_DIR.parent / "daemon_launcher.cmd"
    legacy_launcher.write_text("legacy launcher", encoding="ascii")

    def run(*args):
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)

    assert legacy_launcher.read_text(encoding="ascii") == "legacy launcher"


def test_upgrade_failure_removes_new_task_and_restarts_old_task():
    calls = []

    def run(*args):
        calls.append(args)
        if args[:4] == ("schtasks", "/query", "/tn", backend.LEGACY_TASK_NAME):
            return _ok(stdout="Running")
        if args == ("schtasks", "/run", "/tn", backend.TASK_NAME):
            return _ok(returncode=1, stdout="new failed")
        return _ok()

    with patch.object(backend, "_run", side_effect=run):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)

    assert ("schtasks", "/delete", "/tn", backend.TASK_NAME, "/f") in calls
    assert ("schtasks", "/run", "/tn", backend.LEGACY_TASK_NAME) in calls


def test_status_not_installed():
    with patch.object(backend, "is_installed", return_value=False):
        assert backend.status() == {"installed": False, "running": False, "pid": None}


def test_status_running_when_pidfile_matches_a_live_process():
    backend.PID_FILE.write_text("4242")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True):
        assert backend.status() == {"installed": True, "running": True, "pid": 4242}


def test_status_not_running_when_pidfile_is_stale():
    backend.PID_FILE.write_text("4242")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=False):
        assert backend.status() == {"installed": True, "running": False, "pid": None}


def test_status_not_running_when_no_pidfile():
    with patch.object(backend, "is_installed", return_value=True):
        assert backend.status() == {"installed": True, "running": False, "pid": None}


def test_stop_requires_installed():
    with patch.object(backend, "is_installed", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.stop()


def test_stop_raises_when_no_live_process_found():
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.stop()


def test_stop_taskkills_the_pid_from_the_pidfile():
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True), \
         patch.object(backend, "_pid_is_ours", return_value=True), \
         patch.object(backend, "_run", return_value=_ok()) as mock_run:
        backend.stop()
    mock_run.assert_called_once_with("taskkill", "/pid", "4242", "/f")


def test_stop_refuses_to_kill_a_recycled_pid_that_is_not_ours():
    """Windows reuses pids aggressively and our pidfile goes stale on unclean
    shutdown — force-killing whatever inherited the number would be a bug."""
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True), \
         patch.object(backend, "_pid_is_ours", return_value=False), \
         patch.object(backend, "_run", return_value=_ok()) as mock_run:
        with pytest.raises(backend.DaemonInstallerError, match="recycled"):
            backend.stop()
    mock_run.assert_not_called()


def test_pid_is_ours_requires_a_python_image():
    with patch.object(backend, "_tasklist_line",
                      return_value="python.exe                   4242 Console   1   50,000 K\n"):
        assert backend._pid_is_ours(4242) is True
    with patch.object(backend, "_tasklist_line",
                      return_value="explorer.exe                 4242 Console   1   90,000 K\n"):
        assert backend._pid_is_ours(4242) is False


def test_start_requires_installed():
    with patch.object(backend, "is_installed", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.start()


def test_console_programs_are_launched_without_a_window():
    """The daemon runs hidden (no console), so every console program it launches
    — schtasks/tasklist/taskkill, fired once a second by the Dashboard's status
    poll — must pass CREATE_NO_WINDOW, or Windows allocates a new console window
    for each one (the "console flashing once a second" bug). This is invisible on
    macOS/Linux, where CREATE_NO_WINDOW does not exist and _NO_WINDOW is 0, so
    guard the flag explicitly rather than relying on a real Windows run to catch
    a regression."""
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return _ok()

    with patch.object(backend.subprocess, "run", side_effect=fake_run):
        backend.is_installed()   # runs `schtasks /query` through _run

    assert "creationflags" in seen
    assert seen["creationflags"] == backend._NO_WINDOW
