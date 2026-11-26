import os
import sqlite3
from pathlib import Path

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


def _wal_database(directory):
    db = directory / "claude_unlimited.db"
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma wal_autocheckpoint=0")
    conn.execute("create table marker(value text)")
    conn.execute("insert into marker values ('kept')")
    conn.commit()
    return conn


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


def test_wal_reader_skips_migration_and_keeps_legacy_directory(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    writer = _wal_database(legacy)
    reader = sqlite3.connect(legacy / "claude_unlimited.db")
    reader.execute("begin")
    assert reader.execute("select value from marker").fetchone() == ("kept",)

    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not legacy.is_symlink()
    assert not (tmp_path / ".tokenwell").exists()

    reader.rollback()
    reader.close()
    writer.close()


def test_wal_pending_frames_migrate_with_data_intact(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    writer = _wal_database(legacy)

    chosen = config.migrate_legacy_app_dir()
    assert chosen == tmp_path / ".tokenwell"
    assert (chosen / "tokenwell.db").exists()
    assert (chosen / "tokenwell.db-wal").exists()
    conn = sqlite3.connect(chosen / "tokenwell.db")
    assert conn.execute("select value from marker").fetchone() == ("kept",)
    conn.close()
    writer.close()


def test_wal_growth_after_checkpoint_rolls_back_to_legacy_layout(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    writer = _wal_database(legacy)

    def legacy_writer_after_checkpoint(directory):
        conn = sqlite3.connect(directory / "claude_unlimited.db")
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma wal_autocheckpoint=0")
        conn.execute("insert into marker values ('late')")
        conn.commit()
        conn.close()

    monkeypatch.setattr(config, "_before_legacy_db_rename", legacy_writer_after_checkpoint)
    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not legacy.is_symlink()
    assert (legacy / "claude_unlimited.db").exists()
    assert not (tmp_path / ".tokenwell").exists()
    writer.close()


def test_sidecar_rename_failure_rolls_back_to_legacy_state(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path, db=False)
    writer = _wal_database(legacy)
    real_rename = config.os.rename

    def fail_shm(source, target):
        if Path(source).name == "claude_unlimited.db-shm":
            raise OSError("injected sidecar failure")
        return real_rename(source, target)

    monkeypatch.setattr(config.os, "rename", fail_shm)
    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not legacy.is_symlink()
    assert (legacy / "claude_unlimited.db").exists()
    assert (legacy / "claude_unlimited.db-wal").exists()
    assert (legacy / "claude_unlimited.db-shm").exists()
    assert not (tmp_path / ".tokenwell").exists()
    writer.close()


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


def test_empty_new_dir_does_not_hide_populated_legacy_dir(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    (tmp_path / ".tokenwell").mkdir()

    assert config.migrate_legacy_app_dir() == tmp_path / ".tokenwell"
    assert (tmp_path / ".tokenwell" / "tokenwell.db").exists()
    assert legacy.is_symlink()


def test_nonempty_new_dir_does_not_hide_populated_legacy_dir(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    new = tmp_path / ".tokenwell"
    new.mkdir()
    (new / "unrelated").write_text("keep", encoding="utf-8")

    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not legacy.is_symlink()
    assert new.is_dir()


def test_windows_empty_new_dir_does_not_hide_populated_legacy_dir(tmp_path, monkeypatch):
    _at_home(monkeypatch, tmp_path)
    legacy = _legacy_home(tmp_path)
    new = tmp_path / ".tokenwell"
    new.mkdir()
    monkeypatch.setattr(config.os, "name", "nt")

    assert config.migrate_legacy_app_dir() == legacy
    assert legacy.is_dir() and not legacy.is_symlink()
