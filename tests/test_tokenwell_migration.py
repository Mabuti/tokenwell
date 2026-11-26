import os
import sqlite3

import pytest

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from tokenwell import config


def _legacy_home(tmp_path, *, db=True):
    legacy = tmp_path / ".claude-unlimited"
    legacy.mkdir()
    (legacy / "marker").write_text("kept", encoding="utf-8")
    if db:
        conn = sqlite3.connect(legacy / "claude_unlimited.db")
        conn.execute("create table marker(value text)")
        conn.execute("insert into marker values ('kept')")
        conn.commit()
        conn.close()
    return legacy


def _at_home(monkeypatch, tmp_path):
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))


def test_fresh_install_resolves_new_path(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    assert config.migrate_legacy_app_dir() == tmp_path / ".tokenwell"
    assert not (tmp_path / ".tokenwell").exists()


def test_legacy_only_moves_db_and_leaves_symlink(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    chosen = config.migrate_legacy_app_dir()
    assert chosen == tmp_path / ".tokenwell"
    assert (chosen / "marker").read_text() == "kept"
    assert (chosen / "tokenwell.db").exists()
    assert not (chosen / "claude_unlimited.db").exists()
    assert legacy.is_symlink()
    conn = sqlite3.connect(chosen / "tokenwell.db")
    assert conn.execute("select value from marker").fetchone() == ("kept",)
    conn.close()


def test_both_real_dirs_uses_new_and_leaves_legacy_untouched(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    new = tmp_path / ".tokenwell"
    new.mkdir()
    (new / "marker").write_text("new")
    assert config.migrate_legacy_app_dir() == new
    assert legacy.is_dir() and not legacy.is_symlink()
    assert (legacy / "marker").read_text() == "kept"


def test_already_migrated_symlink_uses_new(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    new = tmp_path / ".tokenwell"
    new.mkdir()
    legacy = tmp_path / ".claude-unlimited"
    legacy.symlink_to(new, target_is_directory=True)
    assert config.migrate_legacy_app_dir() == new


def test_live_legacy_daemon_skips_migration(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    (legacy / "daemon.pid").write_text("1234")
    monkeypatch.setattr(config, "_pid_is_alive", lambda pid: True)
    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not (tmp_path / ".tokenwell").exists()


def test_locked_legacy_db_skips_migration(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    conn = sqlite3.connect(legacy / "claude_unlimited.db")
    conn.execute("begin exclusive")
    assert config.migrate_legacy_app_dir() == legacy
    conn.rollback()
    conn.close()


def test_held_migration_lock_skips_migration(tmp_path, monkeypatch):
    if fcntl is None:
        pytest.skip("POSIX file locking")
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    lock_path = tmp_path / ".tokenwell.migrate.lock"
    with lock_path.open("w+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir()


def test_windows_legacy_only_reads_in_place(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    monkeypatch.setattr(config.os, "name", "nt")
    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not (tmp_path / ".tokenwell").exists()
    assert (legacy / "claude_unlimited.db").exists()
