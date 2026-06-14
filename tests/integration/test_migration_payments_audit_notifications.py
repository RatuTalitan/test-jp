"""Integration tests for migration 0003 (payments, audit_trail, notifications).

Task 3.3. These tests apply the real Alembic migration stack to a transactional
SQLite test database (the portable local/test backend; the same migrations run
on PostgreSQL at runtime) and assert the structural guarantees the design calls
for:

  * the three new tables exist after ``upgrade head``;
  * ``payments.order_id`` is UNIQUE (one payment per order);
  * ``payments.utr`` is globally UNIQUE, *and* multiple NULL ``utr`` rows are
    allowed (NULLs are distinct), so offline orders coexist (Req 6.4);
  * ``notifications`` enforces ``UNIQUE(order_id, kind, transition_seq)`` for
    idempotent enqueue (Req 11);
  * foreign keys are enforced (bad ``order_id`` / ``recipient_id`` rejected);
  * the enum CHECK rejects an out-of-domain ``action`` value;
  * ``downgrade 0002`` drops only the three new tables, leaving the 0001/0002
    tables intact and the version stamped back at ``0002_carts_orders``;
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

from marketplace.db.models import (
    AuditAction,
    AuditTrail,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderState,
    Payment,
    Role,
    User,
)

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
}
NEW_TABLES = {"payments", "audit_trail", "notifications"}


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
    return f"sqlite:///{tmp_path / 'test_0003.db'}"


@pytest.fixture()
def migrated_engine(db_url) -> sa.Engine:
    """Apply all migrations up to head, then yield an FK-enabled engine."""
    command.upgrade(_alembic_config(db_url), "head")
    engine = _make_engine(db_url)
    yield engine
    engine.dispose()


def _seed_order(engine: sa.Engine, *, order_number: int = 1001) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one user + one order; return ``(user_id, order_id)``."""
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            User(
                user_id=user_id,
                telegram_user_id=order_number,  # unique-enough per test
                role=Role.CUSTOMER,
            )
        )
        s.flush()  # ensure the user row exists before the FK-bearing order
        s.add(
            Order(
                order_id=order_id,
                order_number=order_number,
                customer_id=user_id,
                state=OrderState.PAYMENT_PENDING,
                total_amount=100,
            )
        )
        s.commit()
    return user_id, order_id


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_new_tables_created_at_head(migrated_engine):
    names = _table_names(migrated_engine)
    assert NEW_TABLES.issubset(names)
    # The earlier slices are still present.
    assert PREEXISTING_TABLES.issubset(names)


# ---------------------------------------------------------------------------
# payments: order_id uniqueness + utr uniqueness + NULL-distinctness
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_payments_order_id_is_unique(migrated_engine):
    _, order_id = _seed_order(migrated_engine)
    with Session(migrated_engine) as s:
        s.add(Payment(order_id=order_id, utr="UTR000000001"))
        s.commit()
    with Session(migrated_engine) as s:
        s.add(Payment(order_id=order_id, utr="UTR000000002"))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.integration
def test_payments_utr_is_globally_unique(migrated_engine):
    _, order_a = _seed_order(migrated_engine, order_number=2001)
    _, order_b = _seed_order(migrated_engine, order_number=2002)
    with Session(migrated_engine) as s:
        s.add(Payment(order_id=order_a, utr="DUPLICATEUTR1"))
        s.commit()
    with Session(migrated_engine) as s:
        # A different order may NOT reuse the same UTR.
        s.add(Payment(order_id=order_b, utr="DUPLICATEUTR1"))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.integration
def test_multiple_null_utr_rows_allowed(migrated_engine):
    """Many offline orders can each carry a NULL utr (NULLs are distinct)."""
    _, order_a = _seed_order(migrated_engine, order_number=3001)
    _, order_b = _seed_order(migrated_engine, order_number=3002)
    _, order_c = _seed_order(migrated_engine, order_number=3003)
    with Session(migrated_engine) as s:
        s.add(Payment(order_id=order_a, utr=None))
        s.add(Payment(order_id=order_b, utr=None))
        s.add(Payment(order_id=order_c, utr=None))
        s.commit()  # must not raise despite three NULL utr values
        count = s.scalar(sa.select(sa.func.count()).select_from(Payment))
    assert count == 3


# ---------------------------------------------------------------------------
# audit_trail: JSON round-trip + FK + enum rejection
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_audit_trail_insert_and_json_roundtrip(migrated_engine):
    user_id, order_id = _seed_order(migrated_engine, order_number=4001)
    detail = {
        "format_version": 1,
        "field": "quantity",
        "old_value": 2,
        "new_value": 5,
    }
    with Session(migrated_engine) as s:
        s.add(
            AuditTrail(
                order_id=order_id,
                action=AuditAction.MODIFY_LINE,
                detail=detail,
                acting_user_id=user_id,
            )
        )
        s.commit()
    with Session(migrated_engine) as s:
        row = s.scalar(sa.select(AuditTrail))
        assert row.detail == detail
        assert row.action is AuditAction.MODIFY_LINE
        assert row.created_at is not None  # NOT NULL DEFAULT now()


@pytest.mark.integration
def test_audit_trail_foreign_key_enforced(migrated_engine):
    user_id, _ = _seed_order(migrated_engine, order_number=4101)
    with Session(migrated_engine) as s:
        s.add(
            AuditTrail(
                order_id=uuid.uuid4(),  # no such order
                action=AuditAction.ADD_LINE,
                detail={"format_version": 1},
                acting_user_id=user_id,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.integration
def test_audit_trail_enum_rejects_unknown_action(migrated_engine):
    """The enum CHECK rejects an out-of-domain action value (raw insert)."""
    user_id, order_id = _seed_order(migrated_engine, order_number=4201)
    with migrated_engine.begin() as conn:
        stmt = text(
            "INSERT INTO audit_trail "
            "(audit_id, order_id, action, detail, acting_user_id) "
            "VALUES (:aid, :oid, :action, :detail, :uid)"
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                stmt,
                {
                    "aid": uuid.uuid4().hex,
                    "oid": order_id.hex,
                    "action": "NOT_A_REAL_ACTION",
                    "detail": "{}",
                    "uid": user_id.hex,
                },
            )


# ---------------------------------------------------------------------------
# notifications: idempotency unique key + FK + enum
# ---------------------------------------------------------------------------
def _notification(order_id, recipient_id, *, seq=1, kind=NotificationKind.STATE_CHANGE):
    return Notification(
        order_id=order_id,
        recipient_id=recipient_id,
        kind=kind,
        transition_seq=seq,
        payload={"format_version": 1, "event": "PLACED"},
        status=NotificationStatus.PENDING,
    )


@pytest.mark.integration
def test_notifications_unique_idempotency_key(migrated_engine):
    user_id, order_id = _seed_order(migrated_engine, order_number=5001)
    with Session(migrated_engine) as s:
        s.add(_notification(order_id, user_id, seq=1))
        s.commit()
    with Session(migrated_engine) as s:
        # Same (order_id, kind, transition_seq) -> rejected (idempotent enqueue).
        s.add(_notification(order_id, user_id, seq=1))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.integration
def test_notifications_distinct_seq_allowed(migrated_engine):
    user_id, order_id = _seed_order(migrated_engine, order_number=5101)
    with Session(migrated_engine) as s:
        s.add(_notification(order_id, user_id, seq=1))
        s.add(_notification(order_id, user_id, seq=2))
        s.add(_notification(order_id, user_id, seq=1, kind=NotificationKind.MODIFIED))
        s.commit()
        count = s.scalar(sa.select(sa.func.count()).select_from(Notification))
    assert count == 3


@pytest.mark.integration
def test_notifications_defaults_status_attempts_next_attempt(migrated_engine):
    user_id, order_id = _seed_order(migrated_engine, order_number=5201)
    with Session(migrated_engine) as s:
        # Insert via raw SQL omitting defaulted columns to exercise server_default.
        conn = s.connection()
        conn.execute(
            text(
                "INSERT INTO notifications "
                "(notification_id, order_id, recipient_id, kind, transition_seq, payload) "
                "VALUES (:nid, :oid, :rid, :kind, :seq, :payload)"
            ),
            {
                "nid": uuid.uuid4().hex,
                "oid": order_id.hex,
                "rid": user_id.hex,
                "kind": "STATE_CHANGE",
                "seq": 7,
                "payload": "{}",
            },
        )
        s.commit()
        row = s.scalar(sa.select(Notification))
        assert row.status is NotificationStatus.PENDING
        assert row.attempts == 0
        assert row.next_attempt_at is not None  # immediately due


@pytest.mark.integration
def test_notifications_recipient_foreign_key_enforced(migrated_engine):
    _, order_id = _seed_order(migrated_engine, order_number=5301)
    with Session(migrated_engine) as s:
        s.add(_notification(order_id, uuid.uuid4(), seq=1))  # no such recipient
        with pytest.raises(IntegrityError):
            s.commit()


# ---------------------------------------------------------------------------
# Upgrade / downgrade round-trip + drift check
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_downgrade_drops_only_new_tables(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    engine = _make_engine(db_url)
    try:
        assert NEW_TABLES.issubset(_table_names(engine))
        command.downgrade(cfg, "0002_carts_orders")
        names = _table_names(engine)
        # The three new tables are gone...
        assert NEW_TABLES.isdisjoint(names)
        # ...and everything from 0001/0002 survives.
        assert PREEXISTING_TABLES.issubset(names)
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0002_carts_orders"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_alembic_check_reports_no_drift(db_url):
    """The ORM metadata matches the migrated schema (no autogenerate diff)."""
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    # command.check raises if it detects any drift; success == no exception.
    command.check(cfg)
