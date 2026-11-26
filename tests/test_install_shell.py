import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


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
