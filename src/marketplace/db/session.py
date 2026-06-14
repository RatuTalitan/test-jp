"""Engine / session factory helpers for the SQLAlchemy runtime (task 4.1).

Centralizes construction of the SQLAlchemy ``Engine`` and ``sessionmaker`` so
the rest of the app (and the SQLAlchemy Unit of Work in
``marketplace.db.repositories``) never hard-codes connection details. The
database URL is resolved from the config loader's ``DB_URL`` (Req 12.5: secrets
come from the environment, never source), matching the URL the Alembic
migration environment reads.

These helpers do **no** schema creation - migrations own the schema. They only
build the connection machinery the per-update transaction boundary uses.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

__all__ = ["make_engine", "make_session_factory", "engine_from_config"]


def make_engine(db_url: str, *, echo: bool = False) -> Engine:
    """Build a SQLAlchemy :class:`Engine` for ``db_url``.

    For SQLite (local/dev/test) foreign-key enforcement is off by default; the
    caller/migration tests enable it where needed. PostgreSQL (runtime) uses the
    driver defaults.
    """
    return create_engine(db_url, echo=echo, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a ``sessionmaker`` bound to ``engine``.

    ``expire_on_commit=False`` keeps already-loaded attributes usable after a
    commit, which suits the "read, map to a detached domain entity, return"
    pattern of the repositories.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def engine_from_config(db_url: Optional[str] = None, *, echo: bool = False) -> Engine:
    """Build an engine, defaulting the URL to the config loader's ``DB_URL``.

    Imported lazily so importing this module never forces configuration to be
    present (e.g. during unit tests that use the in-memory repositories).
    """
    if db_url is None:
        from marketplace.config.loader import get_config

        db_url = get_config().db_url.reveal()
    return make_engine(db_url, echo=echo)
