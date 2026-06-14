"""Smoke tests for the backup and restore scripts (Task 23.3).

Verifies:
  (a) ``scripts.backup`` and ``scripts.restore`` are importable as modules.
  (b) Running the backup script with a SQLite DB_URL creates a backup file.
  (c) No BOT_TOKEN or other secret is committed to version control
      (``.env`` is untracked by git).

Requirements covered: 13.4, 13.5, 13.6
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (a) Importability of scripts modules
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_backup_module_importable():
    """scripts.backup is importable as a Python module."""
    mod = importlib.import_module("scripts.backup")
    assert mod is not None
    # Key public symbols must be present.
    assert hasattr(mod, "run_backup"), "run_backup not found in scripts.backup"
    assert hasattr(mod, "load_backup_config"), "load_backup_config not found in scripts.backup"
    assert hasattr(mod, "BackupConfig"), "BackupConfig not found in scripts.backup"
    assert hasattr(mod, "BackupError"), "BackupError not found in scripts.backup"


@pytest.mark.smoke
def test_restore_module_importable():
    """scripts.restore is importable as a Python module."""
    mod = importlib.import_module("scripts.restore")
    assert mod is not None
    # Key public symbols must be present.
    assert hasattr(mod, "run_restore"), "run_restore not found in scripts.restore"
    assert hasattr(mod, "load_restore_config"), "load_restore_config not found in scripts.restore"
    assert hasattr(mod, "RestoreConfig"), "RestoreConfig not found in scripts.restore"
    assert hasattr(mod, "RestoreError"), "RestoreError not found in scripts.restore"
    assert hasattr(mod, "RESTORE_WARN_THRESHOLD_SECONDS"), (
        "RESTORE_WARN_THRESHOLD_SECONDS not found in scripts.restore"
    )


# ---------------------------------------------------------------------------
# (b) Backup with SQLite DB_URL creates a backup file
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_sqlite_backup_creates_file(tmp_path: Path):
    """Backup script creates a backup file for a SQLite database.

    Uses a real (minimal) SQLite file so the test validates actual copy
    behaviour without mocks.
    """
    from scripts.backup import BackupConfig, run_backup

    # Create a minimal SQLite "database" file.
    db_file = tmp_path / "test_backup.db"
    db_file.write_bytes(b"SQLite format 3\x00")  # minimal SQLite magic header

    backup_dir = tmp_path / "backups"
    db_url = f"sqlite:///{db_file}"

    config = BackupConfig(
        db_url=db_url,
        backup_dir=backup_dir,
        retention_days=30,
    )

    created = run_backup(config)

    # The backup file must exist.
    assert created.exists(), f"Backup file was not created at {created}"
    # It should be inside the backup directory.
    assert created.parent == backup_dir
    # The filename must follow the expected pattern.
    assert created.name.startswith("backup_"), f"Unexpected filename: {created.name}"
    assert created.suffix == ".db", f"Expected .db suffix, got: {created.suffix}"
    # The backup should be non-empty (at least the SQLite magic header).
    assert created.stat().st_size > 0, "Backup file is empty"


@pytest.mark.smoke
def test_sqlite_backup_via_subprocess(tmp_path: Path):
    """Backup script produces a backup when invoked via subprocess (full CLI path)."""
    # Create a minimal SQLite "database" file.
    db_file = tmp_path / "subprocess_test.db"
    db_file.write_bytes(b"SQLite format 3\x00")

    backup_dir = tmp_path / "backups_subprocess"
    db_url = f"sqlite:///{db_file}"

    env = {
        **os.environ,
        "DB_URL": db_url,
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_RETENTION_DAYS": "30",
    }

    # Locate the Python interpreter used by the test runner.
    python = sys.executable

    result = subprocess.run(
        [python, "-m", "scripts.backup"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent),  # project root
    )

    assert result.returncode == 0, (
        f"backup script exited with status {result.returncode}.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    backup_files = list(backup_dir.glob("backup_*.db"))
    assert backup_files, (
        f"No backup file found in {backup_dir}. stdout: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# (c) No secrets committed – .env is untracked by git
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_dot_env_is_not_tracked_by_git():
    """.env (real secrets file) is NOT tracked by git; only .env.example is.

    Verifies Req 12.5: credentials and secret tokens must be stored outside of
    source code and configuration files committed to version control.
    """
    project_root = Path(__file__).parent.parent

    # Check that .env is NOT tracked by git.
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    tracked_env = result.stdout.strip()
    assert tracked_env == "", (
        f".env appears to be tracked by git: {tracked_env!r}. "
        "Secret values must NOT be committed (Req 12.5)."
    )


@pytest.mark.smoke
def test_dot_env_example_is_tracked_by_git():
    """.env.example (names only, no values) IS tracked by git."""
    project_root = Path(__file__).parent.parent

    result = subprocess.run(
        ["git", "ls-files", ".env.example"],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    tracked_example = result.stdout.strip()
    assert tracked_example == ".env.example", (
        f".env.example does not appear to be tracked by git: {tracked_example!r}."
    )


@pytest.mark.smoke
def test_no_bot_token_in_tracked_files():
    r"""No BOT_TOKEN value is hard-coded in any tracked source file.

    Uses ``git grep`` to scan all tracked files for a pattern that would
    indicate a real Telegram bot token (digit-prefix pattern).
    """
    project_root = Path(__file__).parent.parent

    # A real BOT_TOKEN looks like "1234567890:AABBCCdd..." – scan for that
    # pattern in all tracked non-example files, excluding .env.example itself.
    result = subprocess.run(
        [
            "git", "grep", "--name-only", "-E",
            r"[0-9]{9,}:[A-Za-z0-9_-]{35,}",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )

    # git grep exits 0 if matches found, 1 if none found; 128 on error.
    if result.returncode == 0:
        # Matches found – check they are all in .env.example (which contains
        # only key names, no values).
        matches = [line for line in result.stdout.strip().splitlines()
                   if line and line not in (".env.example",)]
        assert not matches, (
            "Possible BOT_TOKEN value found in tracked file(s): "
            + ", ".join(matches)
        )
    elif result.returncode == 1:
        pass  # No matches – good.
    else:
        pytest.skip(f"git grep failed (exit {result.returncode}): {result.stderr}")
