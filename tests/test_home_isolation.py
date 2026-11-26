import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import tokenwell


_HOME_PATH_MODULES = (
    "tokenwell.config",
    "tokenwell.daemon_installer",
    "tokenwell.daemon_installer.linux_systemd",
    "tokenwell.daemon_installer.macos_launchd",
    "tokenwell.daemon_installer.windows_taskscheduler",
    "tokenwell.secret_store",
    "tokenwell.secret_store.linux_secretservice",
    "tokenwell.secret_store.macos_keychain",
    "tokenwell.secret_store.windows_dpapi",
    "tokenwell.hud",
    "tokenwell.updater",
    "tokenwell.daemon",
    "tokenwell.cli",
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_INSTALLED_PACKAGE_DIR = Path(tokenwell.__file__).resolve().parent


def _is_original_home_violation(
    value, original_home, allowed_roots=(_REPOSITORY_ROOT, _INSTALLED_PACKAGE_DIR)
):
    try:
        value.relative_to(original_home)
    except ValueError:
        return False

    for allowed_root in allowed_roots:
        try:
            value.relative_to(allowed_root)
        except ValueError:
            continue
        return False
    return True


def test_import_time_paths_never_use_the_original_home():
    original_home = Path(os.environ["TOKENWELL_TEST_ORIGINAL_HOME"])
    violations = []

    for module_name in _HOME_PATH_MODULES:
        module = __import__(module_name, fromlist=["*"])
        for name, value in vars(module).items():
            if not isinstance(value, Path) or not value.is_absolute():
                continue
            if _is_original_home_violation(value, original_home):
                violations.append(f"{module_name}.{name}={value}")

    assert not violations, "module-level paths escaped the test home: " + "; ".join(violations)


def test_home_isolation_filter_ignores_repo_but_flags_other_paths(tmp_path):
    original_home = tmp_path / "original-home"
    repository_root = original_home / "work" / "tokenwell"
    inside_repository = repository_root / "tokenwell" / "static"
    outside_repository = original_home / ".config" / "tokenwell"

    assert not _is_original_home_violation(
        inside_repository, original_home, (repository_root,)
    )
    assert _is_original_home_violation(outside_repository, original_home)


def _seed_real_install(home):
    database = home / ".claude-unlimited" / "claude_unlimited.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("create table marker(value text)")
        connection.execute("insert into marker values ('untouched')")
        connection.commit()
    finally:
        connection.close()

    unit = home / ".config" / "systemd" / "user" / "claude-unlimited.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\nDescription=existing\n", encoding="utf-8")
    dropin = unit.with_name(unit.name + ".d") / "keyring.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nEnvironment=KEYRING=existing\n", encoding="utf-8")
    return database, unit, dropin


def test_subprocess_suite_cannot_modify_home_seeded_like_a_real_install(tmp_path):
    if os.environ.get("TOKENWELL_HOME_REGRESSION") == "1":
        pytest.skip("the home-isolation subprocess must not recurse")

    home = tmp_path / "developer-home"
    database, unit, dropin = _seed_real_install(home)
    protected = (database, unit, dropin)
    before = {path: (path.exists(), path.read_bytes()) for path in protected}

    root = Path(__file__).resolve().parent.parent
    selected = [
        "tests/test_home_isolation.py",
        "tests/test_daemon_installer_linux_systemd.py",
        "tests/test_daemon_installer_macos.py",
        "tests/test_daemon_installer_windows_taskscheduler.py",
        "tests/test_tokenwell_migration.py",
    ]
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["TOKENWELL_HOME_REGRESSION"] = "1"
    environment.pop("TOKENWELL_NO_MIGRATE", None)
    environment.pop("PYTEST_CURRENT_TEST", None)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selected, "-q", "-p", "no:cacheprovider"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    after = {path: (path.exists(), path.read_bytes()) for path in protected}
    assert after == before
    assert not list(home.glob(".tokenwell*"))
