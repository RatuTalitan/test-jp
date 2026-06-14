"""payments, audit_trail, notifications

Task 3.3 - the third slice of the marketplace schema (design.md -> Data Models
-> Payments, Audit_Trail, Notifications).

Creates:
  * ``payments``      - one payment per order (UNIQUE ``order_id``), a variable-
                        length ``utr`` (TEXT) that is globally UNIQUE across
                        orders via the ``ix_payments_utr`` unique index while
                        remaining NULLABLE with NULLs-distinct semantics (so the
                        many offline orders without a UTR coexist), the
                        ``utr_submitted_at`` timestamp, and the optional
                        ``screenshot_object_key`` object-storage reference - the
                        blob itself never lives in the DB (Req 6.3, 6.4);
  * ``audit_trail``   - APPEND-ONLY log of significant Seller actions: an
                        ``action`` (``audit_action`` enum), a JSONB/JSON
                        ``detail`` document ``{format_version, field, old_value,
                        new_value}``, the ``acting_user_id``, and a NOT NULL
                        ``created_at DEFAULT now()`` (Req 15.12, 17.8). The
                        append-only guarantee is enforced by application policy
                        plus database privileges (INSERT/SELECT only); on
                        PostgreSQL this migration emits the REVOKE/GRANT when an
                        ``-x app_role=...`` argument is supplied (see below);
  * ``notifications`` - delivery + bounded-retry queue with idempotent
                        ``UNIQUE(order_id, kind, transition_seq)``, a
                        ``notification_kind``/``notification_status`` enum pair,
                        a JSONB/JSON ``payload`` (carrying ``format_version``),
                        ``attempts``/``last_attempt_at``/``next_attempt_at`` retry
                        bookkeeping, and the ``(status, next_attempt_at)`` index
                        the retry worker polls (Req 11.3).

The new enum types (``audit_action``, ``notification_kind``,
``notification_status``) are each referenced by exactly one table, so - exactly
as in migrations 0001/0002 - ``CREATE TABLE`` emits the ``CREATE TYPE`` on
PostgreSQL and ``DROP TABLE`` emits the ``DROP TYPE``; on SQLite the enums are
inline CHECK constraints on their columns. The pre-existing ``order_state`` /
``unit`` / ``role`` / ``language`` enums are NOT touched here.

JSON portability: ``detail`` and ``payload`` render as ``JSONB`` on PostgreSQL
and generic ``JSON`` (TEXT-backed) on SQLite, matching the ORM metadata.

Append-only privileges (PostgreSQL runtime): the append-only contract for
``audit_trail`` is ultimately a privilege concern. Because the application's DB
role name is a runtime/deploy detail (not committed), this migration only emits
the privilege statements when the role is provided explicitly, e.g.::

    alembic -x app_role=marketplace_app upgrade head

When supplied (and only on PostgreSQL), it REVOKEs UPDATE/DELETE and GRANTs
INSERT/SELECT on ``audit_trail`` to that role. Otherwise the GRANTs are left to
the deployment's privilege management and the contract is enforced by app policy.

Revision ID: 0003_payments_audit_notifications
Revises: 0002_carts_orders
Create Date: 2024-01-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from marketplace.db.models import (
    AUDIT_ACTION_ENUM_NAME,
    AUDIT_ACTION_VALUES,
    NOTIFICATION_KIND_ENUM_NAME,
    NOTIFICATION_KIND_VALUES,
    NOTIFICATION_STATUS_ENUM_NAME,
    NOTIFICATION_STATUS_VALUES,
)

# revision identifiers, used by Alembic.
revision: str = "0003_payments_audit_notifications"
down_revision: Union[str, None] = "0002_carts_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(values: Sequence[str], name: str) -> sa.Enum:
    """Build a portable Enum type (native ENUM on PostgreSQL; CHECK on SQLite).

    Each enum below is referenced by exactly one table, so the table
    create/drop ops own the ``CREATE TYPE`` / ``DROP TYPE`` lifecycle on
    PostgreSQL - no duplicate type DDL is produced.
    """
    return sa.Enum(*values, name=name, native_enum=True, create_constraint=True)


def _json() -> sa.types.TypeEngine:
    """JSONB on PostgreSQL, generic JSON (TEXT) on SQLite - matches the ORM."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"

    audit_action = _enum(AUDIT_ACTION_VALUES, AUDIT_ACTION_ENUM_NAME)
    notification_kind = _enum(NOTIFICATION_KIND_VALUES, NOTIFICATION_KIND_ENUM_NAME)
    notification_status = _enum(
        NOTIFICATION_STATUS_VALUES, NOTIFICATION_STATUS_ENUM_NAME
    )

    # --- payments (one per order; globally-unique nullable UTR) ------------
    op.create_table(
        "payments",
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("utr", sa.Text(), nullable=True),
        sa.Column("utr_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("screenshot_object_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
            name="fk_payments_order_id_orders",
        ),
        sa.PrimaryKeyConstraint("payment_id", name="pk_payments"),
        # One payment per order.
        sa.UniqueConstraint("order_id", name="uq_payments_order_id"),
    )
    # Global UTR uniqueness across orders; NULLs are distinct on both backends,
    # so many offline orders may each carry a NULL utr (Req 6.4).
    op.create_index("ix_payments_utr", "payments", ["utr"], unique=True)

    # --- audit_trail (APPEND-ONLY) -----------------------------------------
    op.create_table(
        "audit_trail",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("detail", _json(), nullable=False),
        sa.Column("acting_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
            name="fk_audit_trail_order_id_orders",
        ),
        sa.ForeignKeyConstraint(
            ["acting_user_id"],
            ["users.user_id"],
            name="fk_audit_trail_acting_user_id_users",
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_trail"),
    )

    # --- notifications (delivery + bounded retry) --------------------------
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("kind", notification_kind, nullable=False),
        sa.Column("transition_seq", sa.Integer(), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column(
            "status",
            notification_status,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "attempts",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
            name="fk_notifications_order_id_orders",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.user_id"],
            name="fk_notifications_recipient_id_users",
        ),
        sa.PrimaryKeyConstraint("notification_id", name="pk_notifications"),
        # Idempotent enqueue per order/kind/transition (Req 11).
        sa.UniqueConstraint(
            "order_id",
            "kind",
            "transition_seq",
            name="uq_notifications_order_id_kind_transition_seq",
        ),
    )
    # Retry-worker poll: due rows by status then schedule (Req 11.3).
    op.create_index(
        "ix_notifications_status_next_attempt_at",
        "notifications",
        ["status", "next_attempt_at"],
        unique=False,
    )

    # --- append-only privileges for audit_trail (PostgreSQL runtime) -------
    # The append-only contract is enforced by privileges in production. The app
    # role name is a deploy detail, so emit the GRANTs only when it is supplied
    # via `-x app_role=...` and only on PostgreSQL. SQLite has no role/privilege
    # model, so this is intentionally a no-op there (the contract is then upheld
    # by application policy / the repository never issuing UPDATE/DELETE).
    if is_postgresql:
        app_role = context_app_role()
        if app_role:
            op.execute(
                sa.text(f'REVOKE UPDATE, DELETE ON TABLE audit_trail FROM "{app_role}"')
            )
            op.execute(
                sa.text(f'GRANT INSERT, SELECT ON TABLE audit_trail TO "{app_role}"')
            )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_status_next_attempt_at", table_name="notifications"
    )
    # Dropping each table also drops its column-attached enum type on PostgreSQL
    # (notification_kind, notification_status, audit_action).
    op.drop_table("notifications")
    op.drop_table("audit_trail")

    op.drop_index("ix_payments_utr", table_name="payments")
    op.drop_table("payments")


def context_app_role() -> str | None:
    """Return the ``-x app_role=...`` value, if supplied (PostgreSQL GRANTs)."""
    from alembic import context

    return context.get_x_argument(as_dictionary=True).get("app_role")
