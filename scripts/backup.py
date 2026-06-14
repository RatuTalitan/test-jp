"""Database backup script for the Jan Purna (जन पूर्णा) marketplace.

Creates a timestamped backup of the configured database and prunes backups
older than the retention window. Designed to be run standalone (e.g. by cron
or a systemd timer) or imported as a module for testing.

Usage (standalone)::

    python -m scripts.backup

Or via the helper entry-point (see scripts/README.md)::

    cd /app && python -m scripts.backup >> /var/log/backup.log 2>&1

Environment variables
---------------------
DB_URL : str (required)
    SQLAlchemy-style connection URL. Scheme is used to detect the database
    type:
      * ``postgresql://...`` or ``postgres://...`` → pg_dump (subprocess)
      * ``sqlite:///...``                           → shutil.copy

BACKUP_DIR : str (optional, alias: BACKUP_STORAGE)
    Directory in which backup files are written.  Defaults to ``./backups/``.
    The directory is created automatically if it does not exist.

BACKUP_RETENTION_DAYS : int (optional)
    Number of days to retain backup files. Backups older than this threshold
    are deleted.  Default: 30.

Requirements covered
---------------------
13.4 – automated backup at ≤24 h intervals (this script is the mechanism;
       scheduling is done externally via cron/systemd — see backup.cron).
13.6 – on failure: preserves existing stored data, logs error clearly, exits
       with non-zero status so cron/CI detects the failure.
       TODO: integrate with Notification_Service to deliver a Telegram message
       to the Seller when a running bot context is available.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "BackupConfig",
    "load_backup_config",
    "run_backup",
    "BackupError",
]

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

class BackupError(RuntimeError):
    """Raised when the backup operation cannot be completed."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class BackupConfig:
    """Resolved backup configuration.

    Parameters
    ----------
    db_url:
        Database connection URL (required). Never logged.
    backup_dir:
        Directory to store backup files.
    retention_days:
        How many days to keep backup files (default 30).
    """

    def __init__(
        self,
        db_url: str,
        backup_dir: Path,
        retention_days: int = 30,
    ) -> None:
        self.db_url = db_url
        self.backup_dir = backup_dir
        self.retention_days = retention_days

    def __repr__(self) -> str:
        # Never include db_url – it may contain credentials.
        return (
            f"BackupConfig(backup_dir={self.backup_dir!r}, "
            f"retention_days={self.retention_days})"
        )


def load_backup_config(env: dict[str, str] | None = None) -> BackupConfig:
    """Load :class:`BackupConfig` from environment variables.

    Parameters
    ----------
    env:
        Mapping to read from. Defaults to :data:`os.environ`.

    Raises
    ------
    BackupError
        If ``DB_URL`` is missing or blank.
    """
    source = os.environ if env is None else env

    db_url = (source.get("DB_URL") or "").strip()
    if not db_url:
        raise BackupError(
            "DB_URL is required but not set. "
            "Export it before running this script."
        )

    # BACKUP_DIR takes priority; fall back to BACKUP_STORAGE (alias) then default.
    raw_dir = (
        source.get("BACKUP_DIR")
        or source.get("BACKUP_STORAGE")
        or "./backups/"
    ).strip()
    backup_dir = Path(raw_dir)

    raw_retention = (source.get("BACKUP_RETENTION_DAYS") or "30").strip()
    try:
        retention_days = int(raw_retention)
    except ValueError:
        raise BackupError(
            f"BACKUP_RETENTION_DAYS must be an integer, got: {raw_retention!r}"
        )
    if retention_days < 1:
        raise BackupError(
            f"BACKUP_RETENTION_DAYS must be at least 1, got: {retention_days}"
        )

    return BackupConfig(
        db_url=db_url,
        backup_dir=backup_dir,
        retention_days=retention_days,
    )


# ---------------------------------------------------------------------------
# Database-type detection
# ---------------------------------------------------------------------------

def _detect_db_type(db_url: str) -> str:
    """Return ``'postgresql'`` or ``'sqlite'`` based on the URL scheme.

    Raises
    ------
    BackupError
        If the scheme is unrecognised.
    """
    parsed = urlparse(db_url)
    scheme = parsed.scheme.lower().split("+")[0]  # strip driver, e.g. "postgresql+psycopg"
    if scheme in ("postgresql", "postgres"):
        return "postgresql"
    if scheme == "sqlite":
        return "sqlite"
    raise BackupError(
        f"Unsupported database scheme {scheme!r} in DB_URL. "
        "Only 'postgresql' and 'sqlite' are supported."
    )


# ---------------------------------------------------------------------------
# Backup implementations
# ---------------------------------------------------------------------------

def _sqlite_path_from_url(db_url: str) -> str:
    """Extract the file-system path from a sqlite:/// URL.

    Handles both absolute (``sqlite:////abs/path.db``) and relative
    (``sqlite:///rel/path.db``) forms.
    """
    parsed = urlparse(db_url)
    # urlparse places the path in .path; for relative URLs netloc is empty.
    raw_path = parsed.netloc + parsed.path
    if not raw_path:
        raise BackupError(f"Cannot extract SQLite file path from DB_URL: {db_url!r}")
    return raw_path


def _backup_sqlite(db_url: str, dest: Path) -> None:
    """Copy a SQLite database file to *dest* using :func:`shutil.copy`.

    Parameters
    ----------
    db_url:
        ``sqlite:///...`` connection URL.
    dest:
        Destination file path (should end in ``.db``).
    """
    src = _sqlite_path_from_url(db_url)
    src_path = Path(src)
    if not src_path.exists():
        raise BackupError(
            f"SQLite database file not found: {src_path}. "
            "Ensure the application has been initialised before taking a backup."
        )
    shutil.copy2(str(src_path), str(dest))


def _backup_postgresql(db_url: str, dest: Path) -> None:
    """Dump a PostgreSQL database to *dest* using ``pg_dump``.

    The URL is passed via the ``PGPASSWORD`` / libpq env, not as a positional
    argument, to avoid it appearing in the process list.

    Parameters
    ----------
    db_url:
        ``postgresql://...`` connection URL.
    dest:
        Destination file path (should end in ``.sql``).
    """
    cmd = [
        "pg_dump",
        "--format=plain",  # plain SQL – human-readable, easiest to audit
        "--no-password",   # credentials come from PGPASSWORD / .pgpass / URL
        "--file", str(dest),
        db_url,            # pg_dump accepts a full connection string / URI
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            "pg_dump not found on PATH. Install postgresql-client."
        ) from exc

    if result.returncode != 0:
        stderr_excerpt = result.stderr.strip()[:500]
        raise BackupError(
            f"pg_dump exited with status {result.returncode}. "
            f"Stderr: {stderr_excerpt}"
        )


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------

def _prune_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    """Delete backup files older than *retention_days*.

    Only files matching the naming convention ``backup_YYYYMMDD_HHMMSS.*``
    created by this script are considered.

    Returns
    -------
    list[Path]
        Paths of files that were deleted.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    deleted: list[Path] = []

    for candidate in sorted(backup_dir.glob("backup_????????_??????.*")):
        try:
            mtime = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue  # file disappeared between glob and stat; skip
        if mtime < cutoff:
            try:
                candidate.unlink()
                deleted.append(candidate)
                logger.info("Pruned old backup: %s", candidate.name)
            except OSError as exc:
                logger.warning("Could not prune %s: %s", candidate, exc)

    return deleted


# ---------------------------------------------------------------------------
# Main backup orchestrator
# ---------------------------------------------------------------------------

def run_backup(config: BackupConfig) -> Path:
    """Run a single backup according to *config*.

    Steps:
    1. Ensure the backup directory exists.
    2. Detect the database type from ``DB_URL``.
    3. Create a timestamped backup file.
    4. Prune backups older than the retention window.
    5. Log success.

    Parameters
    ----------
    config:
        Resolved backup configuration.

    Returns
    -------
    Path
        Path to the created backup file.

    Raises
    ------
    BackupError
        On any failure. The caller is responsible for logging and exiting.
    """
    config.backup_dir.mkdir(parents=True, exist_ok=True)

    db_type = _detect_db_type(config.db_url)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    if db_type == "sqlite":
        filename = f"backup_{timestamp}.db"
    else:
        filename = f"backup_{timestamp}.sql"

    dest = config.backup_dir / filename

    logger.info("Starting %s backup → %s", db_type, dest)

    if db_type == "sqlite":
        _backup_sqlite(config.db_url, dest)
    else:
        _backup_postgresql(config.db_url, dest)

    _prune_old_backups(config.backup_dir, config.retention_days)

    logger.info("Backup completed: %s", filename)
    return dest


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point when run as ``python -m scripts.backup``."""
    try:
        config = load_backup_config()
        run_backup(config)
    except BackupError as exc:
        logger.error("Backup failed: %s", exc)
        # Exit non-zero so cron / CI detects the failure (Req 13.6).
        # NOTE: Telegram notification to the Seller requires a running bot
        # context (Notification_Service + Bot_Interface). In a standalone
        # cron/CI context that is not available.  The non-zero exit code is
        # the cron-level failure signal; the TODO below tracks wiring to
        # Notification_Service when the bot context is available.
        # TODO: notify Seller via Notification_Service when bot context exists.
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error during backup: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
