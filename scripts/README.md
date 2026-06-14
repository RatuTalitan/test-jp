# Jan Purna – Backup & Restore Scripts

This directory contains operational scripts for database backup, restore, and
maintenance of the Jan Purna (जन पूर्णा) cotton seed oil cake marketplace.

---

## Overview

| Script | Purpose |
|---|---|
| `backup.py` | Creates a timestamped backup of the database and prunes old backups |
| `restore.py` | Restores the database from a backup file and times the operation |
| `backup.cron` | Example cron / systemd timer configuration for automated scheduling |

---

## Prerequisites

- Python 3.11 with the project virtual environment (`.venv/`)
- For **PostgreSQL**: `pg_dump` and `psql` on `PATH` (`postgresql-client` package)
- For **SQLite**: no additional tools needed

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DB_URL` | **Yes** | — | Database connection URL (e.g. `postgresql://user:pass@host/db` or `sqlite:///./app.db`) |
| `BACKUP_DIR` | No | `./backups/` | Directory to store backup files. Alias `BACKUP_STORAGE` also accepted. |
| `BACKUP_RETENTION_DAYS` | No | `30` | Number of days to retain backup files |
| `BACKUP_FILE` | For restore | — | Path to the backup file to restore from (or pass as a CLI arg) |

---

## Running the Backup Script

```bash
# Export required variables
export DB_URL="postgresql://user:pass@localhost/marketplace"
export BACKUP_DIR="/var/backups/jan-purna"

# Run the backup
cd /app
.venv/bin/python -m scripts.backup
```

On success the script prints:

```
Backup completed: backup_20240101_120000.sql
```

Backups older than `BACKUP_RETENTION_DAYS` (default 30) are automatically
pruned after each successful backup.

---

## Running the Restore Drill

```bash
# Export required variables
export DB_URL="postgresql://user:pass@localhost/marketplace"
export BACKUP_FILE="/var/backups/jan-purna/backup_20240101_120000.sql"

# Run the restore
cd /app
.venv/bin/python -m scripts.restore

# Or pass the backup file as a CLI argument
.venv/bin/python -m scripts.restore /var/backups/jan-purna/backup_20240101_120000.sql
```

The restore script prints the elapsed time. If the restore takes longer than
**60 minutes** a warning is printed to stderr (Requirement 13.5).

---

## Scheduling Automated Backups with Cron

See `backup.cron` for the recommended cron line. Install it with `crontab -e`:

```cron
# Run every 12 hours (satisfies Req 13.4: ≤24 h interval)
0 */12 * * * cd /app && python -m scripts.backup >> /var/log/backup.log 2>&1
```

Set `MAILTO=you@example.com` above the cron line to receive email alerts on
non-zero exit (failure detection per Req 13.6).

### Systemd timer alternative

```ini
# /etc/systemd/system/jan-purna-backup.timer
[Unit]
Description=Jan Purna database backup timer

[Timer]
OnCalendar=*-*-* 00,12:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/jan-purna-backup.service
[Unit]
Description=Jan Purna database backup

[Service]
Type=oneshot
WorkingDirectory=/app
EnvironmentFile=/app/.env
ExecStart=/app/.venv/bin/python -m scripts.backup
StandardOutput=append:/var/log/backup.log
StandardError=append:/var/log/backup.log
```

Enable:

```bash
systemctl enable --now jan-purna-backup.timer
```

---

## Durability Guarantees

| Requirement | Guarantee |
|---|---|
| **Req 13.3** | Data retained for at least **365 days** in durable storage (PostgreSQL with PITR, or the backup archive). |
| **Req 13.4** | Automated backup every **≤24 hours** (12-hour cron schedule). |
| **Req 13.5** | Restore completes within **60 minutes**. The restore script measures and reports duration; an SLA warning is emitted if the threshold is exceeded. |
| **Req 13.6** | On backup/restore failure: existing stored data is preserved, error is logged clearly, and the script exits with a non-zero status so cron/CI detects the failure. A TODO exists to integrate with `Notification_Service` for a Telegram alert to the Seller once a running bot context is available. |

---

## Error Handling

Both scripts exit with status `1` on any failure, printing a clear error
message to stderr. This allows cron/CI pipelines to detect failures via the
non-zero exit code (Req 13.6).

For Telegram-based Seller notifications on failure, see the TODO comment in
`backup.py` and `restore.py`. This requires wiring to the project's
`Notification_Service` and is practical only when the bot process is running.
