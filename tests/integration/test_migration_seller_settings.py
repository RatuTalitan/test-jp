"""Integration tests for migration 0004 (seller_settings single-row table).

Task 3.5. These tests apply the real Alembic migration stack to a transactional
SQLite test database (the portable local/test backend; the same migrations run
on PostgreSQL at runtime) and assert the structural guarantees the design calls
for (design.md -> Data Models -> Seller_Settings; Req 6.2, 6.6, 19.1, 19.3,
19.5, 19.7):

  * the ``seller_settings`` table exists after ``upgrade head``, and the
    0001/0002/0003 tables are still present;
  * the migration seeds exactly one settings row with NULL pickup/UPI values and
    the default ``utr_pattern`` (= ``'^[A-Za-z0-9]{12}$'``), schema_version 1;
  * the SINGLETON constraint prevents a second settings row from existing
    (mirrors meta's ``CHECK (meta_id = 1)`` via the UNIQUE ``singleton`` guard);
  * the ``pickup_location`` length CHECK rejects an empty string and a >500-char
    value, while allowing a within-range value and NULL (Req 19.1/19.2);
  * ``downgrade 0003`` drops only ``seller_settings``, leaving the 0001/0002/0003
    tables intact and the version stamped back at
    ``0003_payments_audit_notifications``;
  * ``alembic check`` reports no schema drift between the ORM metadata and the
    migrated database.
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketplace.db.models import DEFAULT_UTR_PATTERN, SellerSettings

# new-project/  <- tests/integration/<this file> -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

PREEXISTING_TABLES = {
    "meta",
    "users",
    "categories",
    "products",
    "carts",
    "cart_items",
    "orders",
    "order_items",
    "payments",
    "audit_trail",
    "notifications",
}
NEW_TABLES = {"seller_settings"}


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _alembic_config(db_url: str) -> Config:
    """Build an Alembic ``Config`` wired to this project and a target DB URL."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # env.py resolves the URL from the `-x db_url=...` argument first.
    cfg.cmd_opts = argparse.Namespace(x=[f"db_url={db_url}"])
    return cfg


def _make_engine(db_url: str) -> sa.Engine:
    """Engine with SQLite FK enforcement turned on (off by default in SQLite)."""
    engine = create_engine(db_url)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):  # pragma: no cover - trivial pragma
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test_0004.db'}"


@pytest.fixture()
def migrated_engine(db_url) -> sa.Engine:
    """Apply all migrations up to head, then yield an FK-enabled engine."""
    command.upgrade(_alembic_config(db_url), "head")
    engine = _make_engine(db_url)
    yield engine
    engine.dispose()


def _settings_id(engine: sa.Engine) -> str:
    """Return the seeded singleton row's primary key (raw stored form)."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT seller_settings_id FROM seller_settings")
        ).scalar_one()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_seller_settings_table_created_at_head(migrated_engine):
    names = _table_names(migrated_engine)
    assert NEW_TABLES.issubset(names)
    # The earlier slices are still present.
    assert PREEXISTING_TABLES.issubset(names)


# ---------------------------------------------------------------------------
# Seed row: one row, NULL pickup/UPI, default utr_pattern, schema_version 1
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_migration_seeds_single_default_row(migrated_engine):
    with Session(migrated_engine) as s:
        rows = s.scalars(sa.select(SellerSettings)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.singleton == 1
    assert row.pickup_location is None
    assert row.upi_address is None
    assert row.upi_qr_object_key is None
    assert row.schema_version == 1


@pytest.mark.integration
def test_utr_pattern_default_is_twelve_alphanumeric(migrated_engine):
    """The seeded ``utr_pattern`` server-default is exactly-12-alphanumeric."""
    with Session(migrated_engine) as s:
        row = s.scalar(sa.select(SellerSettings))
    assert row.utr_pattern == DEFAULT_UTR_PATTERN
    assert row.utr_pattern == r"^[A-Za-z0-9]{12}$"


# ---------------------------------------------------------------------------
# Singleton constraint: at most one settings row can exist
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_singleton_constraint_prevents_second_row(migrated_engine):
    """A second row (singleton = 1) is rejected by the UNIQUE singleton guard."""
    # The migration already seeded the one allowed row; inserting another with
    # the same constant discriminator must violate the singleton uniqueness.
    with Session(migrated_engine) as s:
        s.add(SellerSettings(singleton=1))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.integration
def test_singleton_check_rejects_non_one_discriminator(migrated_engine):
    """The CHECK (singleton = 1) rejects any discriminator other than 1."""
    with migrated_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO seller_settings "
                    "(seller_settings_id, singleton) VALUES (:sid, 2)"
                ),
                {"sid": uuid.uuid4().hex},
            )


# ---------------------------------------------------------------------------
# pickup_location length CHECK: 1-500 when present, NULL allowed
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_pickup_location_rejects_empty_string(migrated_engine):
    sid = _settings_id(migrated_engine)
    with migrated_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "UPDATE seller_settings SET pickup_location = '' "
                    "WHERE seller_settings_id = :sid"
                ),
                {"sid": sid},
            )


@pytest.mark.integration
def test_pickup_location_rejects_over_500_chars(migrated_engine):
    sid = _settings_id(migrated_engine)
    with migrated_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "UPDATE seller_settings SET pickup_location = :loc "
                    "WHERE seller_settings_id = :sid"
                ),
                {"loc": "x" * 501, "sid": sid},
            )


@pytest.mark.integration
def test_pickup_location_allows_within_range_and_null(migrated_engine):
    sid = _settings_id(migrated_engine)
    # A within-range (1-500) value is accepted, including the 500-char boundary.
    for value in ("Main mandi gate, Village Rd", "y" * 500, "z" * 1):
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE seller_settings SET pickup_location = :loc "
                    "WHERE seller_settings_id = :sid"
                ),
                {"loc": value, "sid": sid},
            )
        with Session(migrated_engine) as s:
            assert s.scalar(sa.select(SellerSettings)).pickup_location == value
    # NULL (pickup not yet configured) is allowed (Req 19.9).
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE seller_settings SET pickup_location = NULL "
                "WHERE seller_settings_id = :sid"
            ),
            {"sid": sid},
        )
    with Session(migrated_engine) as s:
        assert s.scalar(sa.select(SellerSettings)).pickup_location is None


# ---------------------------------------------------------------------------
# upi_address / upi_qr_object_key are free-form nullable references
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_upi_fields_store_and_roundtrip(migrated_engine):
    """The column just stores the VPA / object-storage key (validated above)."""
    sid = _settings_id(migrated_engine)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE seller_settings "
                "SET upi_address = :vpa, upi_qr_object_key = :key "
                "WHERE seller_settings_id = :sid"
            ),
            {"vpa": "seller@upi", "key": "qr/seller-upi-qr.png", "sid": sid},
        )
    with Session(migrated_engine) as s:
        row = s.scalar(sa.select(SellerSettings))
    assert row.upi_address == "seller@upi"
    assert row.upi_qr_object_key == "qr/seller-upi-qr.png"


# ---------------------------------------------------------------------------
# Upgrade / downgrade round-trip + drift check
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_downgrade_drops_only_seller_settings(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    engine = _make_engine(db_url)
    try:
        assert NEW_TABLES.issubset(_table_names(engine))
        command.downgrade(cfg, "0003_payments_audit_notifications")
        names = _table_names(engine)
        # seller_settings is gone...
        assert NEW_TABLES.isdisjoint(names)
        # ...and everything from 0001/0002/0003 survives.
        assert PREEXISTING_TABLES.issubset(names)
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0003_payments_audit_notifications"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_alembic_check_reports_no_drift(db_url):
    """The ORM metadata matches the migrated schema (no autogenerate diff)."""
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    # command.check raises if it detects any drift; success == no exception.
    command.check(cfg)
