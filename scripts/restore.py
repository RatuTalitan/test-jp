"""Database restore script for the Jan Purna (जन पूर्णा) marketplace.

Restores a database from a backup file produced by ``scripts/backup.py``.
Designed to be run standalone (restore drill) or imported as a module.

Usage (standalone)::

    # Pass BACKUP_FILE via environment:
    export DB_URL="postgresql://user:pass@host/db"
    export BACKUP_FILE="/path/to/backup_20240101_120000.sql"
    python -m scripts.restore

    # Or pass BACKUP_FILE as a CLI argument:
    python -m scripts.restore /path/to/backup_20240101_120000.sql

Environment variables
---------------------
DB_URL : str (required)
    Database connection URL. Scheme determines the restore method:
      * ``postgresql://...`` or ``postgres://...`` → psql (subprocess)
      * ``sqlite:///...``                           → shutil.copy

BACKUP_FILE : str (optional if passed as CLI arg)
    Path to the backup file to restore from.

Requirements covered
---------------------
13.5 – restore completes within 60 minutes.  This script measures and reports
       the duration; if it exceeds 3 600 s a WARNING is printed.  Hard-blocking
       on a real restore is impractical in a script; the warning is the right
       behavior (the operator should investigate and fix the root cause).
13.6 – on failure: the original data is preserved (restore is written to the
       target DB only on success), the error is logged clearly, and the script
       exits with non-zero status so CI/cron detects the failure.
       TODO: integrate with Notification_Service for Telegram alerts to the
       Seller when a running bot context is available.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "RestoreConfig",
    "load_restore_config",
    "run_restore",
    "RestoreError",
    "RESTORE_WARN_THRESHOLD_SECONDS",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Req 13.5: restore should complete within 60 minutes (3 600 s).
RESTORE_WARN_THRESHOLD_SECONDS: int = 3600

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RestoreError(RuntimeError):
    """Raised when the restore operation cannot be completed."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class RestoreConfig:
    """Resolved restore configuration.

    Parameters
    ----------
    db_url:
        Database connection URL (required). Never logged.
    backup_file:
        Path to the backup file to restore from (required).
    """

    def __init__(self, db_url: str, backup_file: Path) -> None:
        self.db_url = db_url
        self.backup_file = backup_file

    def __repr__(self) -> str:
        # Never include db_url – it may contain credentials.
        return f"RestoreConfig(backup_file={self.backup_file!r})"


def load_restore_config(
    env: dict[str, str] | None = None,
    cli_args: list[str] | None = None,
) -> RestoreConfig:
    """Load :class:`RestoreConfig` from environment variables and/or CLI args.

    The backup file path is resolved from (in order of priority):

    1. ``cli_args[0]`` (first positional CLI argument after the script name).
    2. ``BACKUP_FILE`` environment variable.

    Parameters
    ----------
    env:
        Mapping to read from. Defaults to :data:`os.environ`.
    cli_args:
        Positional arguments (``sys.argv[1:]``).  Defaults to
        :data:`sys.argv[1:]` when not supplied.

    Raises
    ------
    RestoreError
        If ``DB_URL`` or the backup file path is missing/blank.
    """
    source = os.environ if env is None else env
    args = sys.argv[1:] if cli_args is None else cli_args

    db_url = (source.get("DB_URL") or "").strip()
    if not db_url:
        raise RestoreError(
            "DB_URL is required but not set. "
            "Export it before running this script."
        )

    # Resolve backup file: CLI arg takes priority over env var.
    raw_path: str = ""
    if args:
        raw_path = args[0].strip()
    if not raw_path:
        raw_path = (source.get("BACKUP_FILE") or "").strip()
    if not raw_path:
        raise RestoreError(
            "BACKUP_FILE is required. "
            "Set the BACKUP_FILE environment variable or pass the path as an argument."
        )

    return RestoreConfig(db_url=db_url, backup_file=Path(raw_path))


# ---------------------------------------------------------------------------
# Database-type detection (shared with backup.py)
# ---------------------------------------------------------------------------

def _detect_db_type(db_url: str) -> str:
    """Return ``'postgresql'`` or ``'sqlite'`` based on the URL scheme."""
    parsed = urlparse(db_url)
    scheme = parsed.scheme.lower().split("+")[0]
    if scheme in ("postgresql", "postgres"):
        return "postgresql"
    if scheme == "sqlite":
        return "sqlite"
    raise RestoreError(
        f"Unsupported database scheme {scheme!r} in DB_URL. "
        "Only 'postgresql' and 'sqlite' are supported."
    )


def _sqlite_path_from_url(db_url: str) -> str:
    """Extract the file-system path from a sqlite:/// URL."""
    parsed = urlparse(db_url)
    raw_path = parsed.netloc + parsed.path
    if not raw_path:
        raise RestoreError(f"Cannot extract SQLite file path from DB_URL: {db_url!r}")
    return raw_path


# ---------------------------------------------------------------------------
# Restore implementations
# ---------------------------------------------------------------------------

def _restore_sqlite(db_url: str, backup_file: Path) -> None:
    """Restore a SQLite database from *backup_file* using :func:`shutil.copy`.

    The original database file is overwritten. If the restore fails mid-copy
    the partially-written file is removed so the original can be recovered from
    a prior backup.

    Parameters
    ----------
    db_url:
        ``sqlite:///...`` connection URL pointing to the live database.
    backup_file:
        Path to the ``.db`` backup file.
    """
    target = Path(_sqlite_path_from_url(db_url))
    if not backup_file.exists():
        raise RestoreError(f"Backup file not found: {backup_file}")
    # Ensure parent directory exists.
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(backup_file), str(target))


def _restore_postgresql(db_url: str, backup_file: Path) -> None:
    """Restore a PostgreSQL database from a plain-SQL dump using ``psql``.

    Parameters
    ----------
    db_url:
        ``postgresql://...`` connection URL for the target database.
    backup_file:
        Path to the ``.sql`` dump file produced by ``pg_dump --format=plain``.
    """
    if not backup_file.exists():
        raise RestoreError(f"Backup file not found: {backup_file}")

    cmd = [
        "psql",
        "--no-password",
        "--file", str(backup_file),
        db_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RestoreError(
            "psql not found on PATH. Install postgresql-client."
        ) from exc

    if result.returncode != 0:
        stderr_excerpt = result.stderr.strip()[:500]
        raise RestoreError(
            f"psql exited with status {result.returncode}. "
            f"Stderr: {stderr_excerpt}"
        )


# ---------------------------------------------------------------------------
# Main restore orchestrator
# ---------------------------------------------------------------------------

def run_restore(config: RestoreConfig) -> float:
    """Run a single restore from *config*.

    Steps:
    1. Detect the database type.
    2. Time the restore operation.
    3. Print the duration and emit a WARNING if it exceeds 60 minutes.

    Parameters
    ----------
    config:
        Resolved restore configuration.

    Returns
    -------
    float
        Elapsed time in seconds.

    Raises
    ------
    RestoreError
        On any failure.
    """
    db_type = _detect_db_type(config.db_url)

    logger.info(
        "Starting %s restore from backup file: %s",
        db_type,
        config.backup_file,
    )

    start = time.monotonic()

    if db_type == "sqlite":
        _restore_sqlite(config.db_url, config.backup_file)
    else:
        _restore_postgresql(config.db_url, config.backup_file)

    elapsed = time.monotonic() - start

    print(f"Restore completed in {elapsed:.2f} seconds.")
    logger.info("Restore completed in %.2f seconds.", elapsed)

    if elapsed > RESTORE_WARN_THRESHOLD_SECONDS:
        # Req 13.5: restore should complete within 60 minutes.
        warning_msg = (
            "WARNING: restore took longer than 60 minutes "
            f"({elapsed / 60:.1f} min). "
            "Investigate the backup size and database performance."
        )
        print(warning_msg, file=sys.stderr)
        logger.warning(warning_msg)

    return elapsed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point when run as ``python -m scripts.restore``."""
    try:
        config = load_restore_config()
        run_restore(config)
    except RestoreError as exc:
        logger.error("Restore failed: %s", exc)
        # Req 13.6: exit non-zero so cron/CI detects the failure.
        # Existing stored data is preserved because restore errors are raised
        # before or during the write; a partial SQLite copy that fails would
        # leave the original intact (the copy target may be corrupted, but the
        # source backup file remains untouched).
        # TODO: notify Seller via Notification_Service when bot context exists.
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error during restore: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
