"""Alembic migration environment for the marketplace.

Wires the migration runner to:
  * the project's SQLAlchemy metadata (``marketplace.db.Base.metadata``) as the
    autogenerate ``target_metadata``, and
  * a database URL resolved from the **config loader's DB_URL** notion, so the
    same migrations run against SQLite locally/in tests and PostgreSQL at
    runtime (design.md -> "Managed PostgreSQL ...; SQLite for local dev").

URL resolution order (highest priority first):
  1. ``-x db_url=...`` on the alembic command line (used by the migration test).
  2. the ``DB_URL`` environment variable - the exact variable name the
     application config loader reads (``marketplace.config.loader.ENV_DB_URL``).
  3. the ``sqlalchemy.url`` value in ``alembic.ini`` (local-dev fallback).

No connection secret is hard-coded here (Req 12.5).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the application's metadata (single source of truth for the schema).
from marketplace.db import Base

# The DB_URL env-var name is owned by the config loader; reuse it so the
# migration environment is "wired to the DB_URL from the config loader".
try:  # pragma: no cover - defensive: loader is part of the project (task 2.1)
    from marketplace.config.loader import ENV_DB_URL
except Exception:  # pragma: no cover
    ENV_DB_URL = "DB_URL"

import os

# Alembic Config object, providing access to values in alembic.ini.
config = context.config

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Resolve the database URL per the documented priority order."""
    # 1. `-x db_url=...`
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db_url"):
        return x_args["db_url"]
    # 2. DB_URL environment variable (config-loader variable name).
    env_url = os.environ.get(ENV_DB_URL)
    if env_url:
        return env_url
    # 3. alembic.ini fallback.
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    raise RuntimeError(
        "No database URL configured: pass `-x db_url=...`, set the DB_URL "
        "environment variable, or set sqlalchemy.url in alembic.ini."
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # SQLite cannot ALTER most things; batch mode makes future migrations
        # portable (no effect on plain CREATE TABLE statements).
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    url = _resolve_url()
    # Inject the resolved URL so engine_from_config builds the right engine.
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
