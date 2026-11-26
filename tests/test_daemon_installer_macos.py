import os
import subprocess

import pytest

import tokenwell.daemon_installer.macos_launchd as launchd

# The launchd backend is macOS's. These assert its plist layout and launchctl
# invocations, so on Windows they are not a failure, they are inapplicable —
# and a Windows run whose failures are all inapplicable tests tells nobody
# anything about Windows.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="macOS launchd backend")


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(launchd, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "LaunchAgents" / f"{launchd.LABEL}.plist")
    monkeypatch.setattr(launchd, "LEGACY_PLIST_PATH", tmp_path / "LaunchAgents" / f"{launchd.LEGACY_LABEL}.plist")
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "logs")
    return tmp_path


class FakeLaunchctl:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, *args):
        self.calls.append(args)
        for prefix, result in self.responses.items():
            if args[: len(prefix)] == prefix:
                return result
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_is_installed_false_before_install(isolated_paths):
    assert launchd.is_installed() is False


def test_install_writes_plist_and_bootstraps(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)

    launchd.install(port=4317)

    assert launchd.is_installed() is True
    assert launchd.LOG_DIR.exists()
    assert any(c[0] == "bootstrap" for c in fake.calls)
    assert any(c[0] == "bootout" for c in fake.calls)  # unload-first for idempotent re-install

    import plistlib

    plist = plistlib.loads(launchd.PLIST_PATH.read_bytes())
    assert plist["Label"] == launchd.LABEL
    assert plist["ProgramArguments"][-2:] == ["--port", "4317"]
    assert plist["RunAtLoad"] is True


def test_rewrite_service_paths_after_migration_updates_plist_only(isolated_paths, monkeypatch):
    home = isolated_paths / "home"
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: home))
    import plistlib

    old_root = home / ".claude-unlimited"
    new_root = home / ".tokenwell"
    isolated_paths.joinpath("LaunchAgents").mkdir(parents=True, exist_ok=True)
    launchd.PLIST_PATH.write_bytes(plistlib.dumps({
        "StandardOutPath": str(old_root / "logs" / "daemon.out.log"),
        "StandardErrorPath": str(old_root / "logs" / "daemon.err.log"),
    }))
    calls = []
    monkeypatch.setattr(launchd, "_run_launchctl", lambda *args: calls.append(args))

    launchd.rewrite_service_paths_after_migration()

    plist = plistlib.loads(launchd.PLIST_PATH.read_bytes())
    assert plist["StandardOutPath"] == str(new_root / "logs" / "daemon.out.log")
    assert plist["StandardErrorPath"] == str(new_root / "logs" / "daemon.err.log")
    assert calls == []


def test_install_raises_on_bootstrap_failure(isolated_paths, monkeypatch):
    fail = subprocess.CompletedProcess(["launchctl"], 1, stdout="", stderr="boom")
    fake = FakeLaunchctl(responses={("bootstrap",): fail})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)

    with pytest.raises(launchd.DaemonInstallerError, match="boom"):
        launchd.install(port=4317)


def test_upgrade_removes_old_plist_only_after_new_bootstrap(isolated_paths, monkeypatch):
    old = isolated_paths / "LaunchAgents" / f"{launchd.LEGACY_LABEL}.plist"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)

    launchd.install(port=4317)

    assert not old.exists()
    assert launchd.PLIST_PATH.exists()
    bootout_old = next(i for i, call in enumerate(fake.calls)
                       if call[0] == "bootout" and launchd.LEGACY_LABEL in call[-1])
    bootstrap_new = next(i for i, call in enumerate(fake.calls) if call[0] == "bootstrap")
    assert bootout_old < bootstrap_new


def test_upgrade_failure_restores_running_old_plist(isolated_paths, monkeypatch):
    old = isolated_paths / "LaunchAgents" / f"{launchd.LEGACY_LABEL}.plist"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    calls = []
    bootstrap_count = 0

    def run(*args):
        nonlocal bootstrap_count
        calls.append(args)
        if args[0] == "bootstrap":
            bootstrap_count += 1
            if bootstrap_count == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", run)
    with pytest.raises(launchd.DaemonInstallerError, match="boom"):
        launchd.install(port=4317)

    assert old.exists()
    assert not launchd.PLIST_PATH.exists()
    assert ("bootstrap", launchd._domain_target(), str(old)) in calls


def test_failed_reinstall_restores_existing_new_plist_and_loaded_state(isolated_paths, monkeypatch):
    launchd.PLIST_PATH.parent.mkdir(parents=True)
    launchd.PLIST_PATH.write_bytes(b"previous plist")
    calls = []
    bootstrap_count = 0

    def run(*args):
        nonlocal bootstrap_count
        calls.append(args)
        if args[0] == "print" and launchd.LABEL in args[-1]:
            return subprocess.CompletedProcess(args, 0, stdout="loaded", stderr="")
        if args[0] == "bootstrap":
            bootstrap_count += 1
            if bootstrap_count == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", run)
    with pytest.raises(launchd.DaemonInstallerError, match="boom"):
        launchd.install(port=4317)

    assert launchd.PLIST_PATH.read_bytes() == b"previous plist"
    assert calls.count(("bootstrap", launchd._domain_target(), str(launchd.PLIST_PATH))) == 2


def test_failed_reinstall_attempts_legacy_restore_when_new_definition_restore_fails(isolated_paths, monkeypatch):
    old = isolated_paths / "LaunchAgents" / f"{launchd.LEGACY_LABEL}.plist"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    launchd.PLIST_PATH.write_bytes(b"previous plist")
    calls = []
    bootstrap_new_count = 0

    def run(*args):
        nonlocal bootstrap_new_count
        calls.append(args)
        if args[0] == "print" and launchd.LEGACY_LABEL in args[-1]:
            return subprocess.CompletedProcess(args, 0, stdout="loaded", stderr="")
        if args[0] == "print" and launchd.LABEL in args[-1]:
            return subprocess.CompletedProcess(args, 0, stdout="loaded", stderr="")
        if args[0] == "bootstrap" and str(launchd.PLIST_PATH) in args:
            bootstrap_new_count += 1
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="restore failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", run)
    with pytest.raises(launchd.DaemonInstallerError, match="restore failed"):
        launchd.install(port=4317)

    assert bootstrap_new_count == 2
    assert ("bootstrap", launchd._domain_target(), str(old)) in calls


def test_failed_upgrade_does_not_restart_legacy_after_data_migration(isolated_paths, monkeypatch):
    home = isolated_paths / "home"
    home.mkdir()
    new = home / ".tokenwell"
    new.mkdir()
    (new / "tokenwell.db").write_bytes(b"db")
    (home / ".claude-unlimited").symlink_to(new, target_is_directory=True)
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: home))
    old = isolated_paths / "LaunchAgents" / f"{launchd.LEGACY_LABEL}.plist"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "print" and launchd.LEGACY_LABEL in args[-1]:
            return subprocess.CompletedProcess(args, 0, stdout="loaded", stderr="")
        if args[0] == "bootstrap" and str(launchd.PLIST_PATH) in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="new failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", run)
    with pytest.raises(launchd.DaemonInstallerError):
        launchd.install(port=4317)

    assert ("bootstrap", launchd._domain_target(), str(old)) not in calls


def test_uninstall_removes_plist_and_boots_out(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    assert launchd.is_installed() is True

    launchd.uninstall()
    assert launchd.is_installed() is False
    assert any(c[0] == "bootout" for c in fake.calls)


def test_uninstall_when_never_installed_does_not_raise(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.uninstall()  # must not raise


def test_status_not_installed(isolated_paths):
    assert launchd.status() == {"installed": False, "running": False, "pid": None}


def test_status_installed_but_not_running(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)

    not_running = subprocess.CompletedProcess(["launchctl"], 0, stdout="state = not running\n", stderr="")
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl(responses={("print",): not_running}))
    result = launchd.status()
    assert result == {"installed": True, "running": False, "pid": None}


def test_status_installed_and_running_parses_pid(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)

    running = subprocess.CompletedProcess(["launchctl"], 0, stdout="state = running\n\tpid = 12345\n", stderr="")
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl(responses={("print",): running}))
    result = launchd.status()
    assert result == {"installed": True, "running": True, "pid": 12345}


def test_start_requires_install(isolated_paths, monkeypatch):
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl())
    with pytest.raises(launchd.DaemonInstallerError, match="install"):
        launchd.start()


def test_start_kickstarts_when_installed(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.start()
    assert any(c[0] == "kickstart" for c in fake.calls)


def test_stop_requires_install(isolated_paths, monkeypatch):
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl())
    with pytest.raises(launchd.DaemonInstallerError, match="install|Not installed"):
        launchd.stop()


def test_stop_kills_when_installed(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.stop()
    assert any(c[0] == "kill" for c in fake.calls)


def test_reinstall_with_different_port_updates_plist(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.install(port=5000)

    import plistlib

    plist = plistlib.loads(launchd.PLIST_PATH.read_bytes())
    assert plist["ProgramArguments"][-2:] == ["--port", "5000"]
