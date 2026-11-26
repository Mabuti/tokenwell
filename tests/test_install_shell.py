import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _installer_stub_path(tmp_path):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stubs = {
        "uname": "printf '%s\\n' Linux\n",
        "mktemp": "printf '%s\\n' \"$HOME/installer-temp\"\n",
        "rm": "exit 0\n",
        "env": "if [ \"$1\" = bash ]; then shift; exec /bin/bash \"$@\"; fi\nexit 1\n",
        "bash": "exec /bin/bash \"$@\"\n",
    }
    for name, body in stubs.items():
        stub = stub_dir / name
        stub.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
        stub.chmod(0o755)
    return stub_dir


def _run_real_installer(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir()
    stub_dir = _installer_stub_path(tmp_path)
    env = {"HOME": str(home), "PATH": str(stub_dir)}
    bash = shutil.which("bash")
    assert bash is not None

    if mode == "bash-stdin":
        command = [bash]
        input_data = (ROOT / "install.sh").read_bytes()
    elif mode == "cat-pipe":
        command = [
            "/bin/sh", "-c",
            f"/bin/cat {shlex.quote(str(ROOT / 'install.sh'))} | {shlex.quote(bash)}",
        ]
        input_data = None
    elif mode == "bash-file":
        command = [bash, "install.sh"]
        input_data = None
    else:
        command = [str(ROOT / "install.sh")]
        input_data = None

    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        input=input_data,
        capture_output=True,
        text=False,
    )


@pytest.mark.parametrize("mode", ["bash-stdin", "cat-pipe", "bash-file", "direct-file"])
def test_installer_executes_main_when_run_as_a_script_or_from_stdin(tmp_path, mode):
    result = _run_real_installer(tmp_path, mode)

    assert result.returncode == 1
    assert b"Tokenwell installer" in result.stdout
    assert b"Missing required command(s):" in result.stderr


@pytest.fixture
def systemctl_stub(tmp_path):
    calls = tmp_path / "systemctl.calls"
    stub = tmp_path / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_CALLS\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, calls


def _run_restore(tmp_path, layout, systemctl_stub):
    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".claude-unlimited"
    new = home / ".tokenwell"
    if layout in ("legacy", "both", "legacy-without-db"):
        legacy.mkdir()
    if layout in ("legacy", "both"):
        (legacy / "claude_unlimited.db").write_bytes(b"legacy")
    if layout == "legacy-without-db":
        (legacy / "marker").write_text("legacy", encoding="utf-8")
    if layout in ("symlink", "both", "legacy-without-db"):
        new.mkdir()
        (new / "tokenwell.db").write_bytes(b"new")
    if layout == "symlink":
        legacy.symlink_to(new, target_is_directory=True)

    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "marker").write_text("remove", encoding="utf-8")
    stub, calls = systemctl_stub
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": str(stub.parent) + os.pathsep + env["PATH"],
        "SYSTEMCTL_CALLS": str(calls),
    })
    command = """
set -euo pipefail
source "$INSTALL_SCRIPT"
LEGACY_SERVICE_STOPPED=1
CLONED="$CLONE_DIR"
on_exit
"""
    return subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**env, "INSTALL_SCRIPT": str(ROOT / "install.sh"), "CLONE_DIR": str(clone)},
        capture_output=True,
        text=True,
    ), calls, clone


@pytest.mark.parametrize(
    ("layout", "should_start", "should_report_migrated"),
    [
        ("legacy", True, False),
        ("symlink", False, True),
        ("both", True, False),
        ("legacy-without-db", False, True),
    ],
)
def test_install_restore_trap_handles_legacy_layouts(
    tmp_path, systemctl_stub, layout, should_start, should_report_migrated
):
    result, calls, clone = _run_restore(tmp_path, layout, systemctl_stub)

    assert result.returncode == 0, result.stderr
    assert not clone.exists()
    call_text = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert ("--user start claude-unlimited.service" in call_text) is should_start
    assert ("Legacy data has been migrated" in result.stderr) is should_report_migrated
