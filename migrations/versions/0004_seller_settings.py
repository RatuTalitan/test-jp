"""seller_settings single-row table

Task 3.5 - the fourth slice of the marketplace schema (design.md -> Data Models
-> Seller_Settings; Req 6.2, 6.6, 19.1, 19.3, 19.5, 19.7).

Creates the ``seller_settings`` table: a SINGLE, Seller-owned settings row
holding the operator-configurable values that were previously pure environment
configuration -

  * ``pickup_location``    - Seller-defined collection address; NULL until set,
                             with a CHECK enforcing 1-500 chars when present
                             (Req 19.1/19.2, may be null initially per 19.9);
  * ``upi_address``        - payee VPA; NULL until set (VPA shape validated at
                             the service layer in task 20.2 - the column only
                             stores it, Req 19.3);
  * ``upi_qr_object_key``  - object-storage *reference* to the uploaded UPI QR
                             image; the blob never lives in the DB (Req 19.5);
  * ``utr_pattern``        - the configurable UTR acceptance format, TEXT NOT
                             NULL DEFAULT ``'^[A-Za-z0-9]{12}$'`` (exactly 12
                             alphanumeric, Seller/operator-adjustable, Req
                             6.2/6.6);
  * ``updated_at``         - last-change timestamp (TIMESTAMPTZ);
  * ``schema_version``     - SMALLINT DEFAULT 1 (Req 13.7).

Singleton: enforced exactly like migration 0001's ``meta`` table
(``CHECK (meta_id = 1)``) - a constant discriminator column ``singleton`` pinned
to 1 by a CHECK and made UNIQUE, so at most one settings row can ever exist
regardless of the UUID primary key. The migration also *seeds* the singleton row
with NULL pickup/UPI values and the default ``utr_pattern`` so reads always find
the authoritative row on first boot.

Portability: the table uses the same generic types as the rest of the schema
(``Uuid`` -> CHAR(32) on SQLite, ``DateTime(timezone=True)``, ``SmallInteger``)
so the identical migration runs on SQLite (local/test) and PostgreSQL (runtime).
No new enum types are introduced. The pre-existing 0001/0002/0003 tables are not
touched.

Revision ID: 0004_seller_settings
Revises: 0003_payments_audit_notifications
Create Date: 2024-01-04 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from marketplace.db.models import DEFAULT_UTR_PATTERN

# revision identifiers, used by Alembic.
revision: str = "0004_seller_settings"
down_revision: Union[str, None] = "0003_payments_audit_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "seller_settings",
        sa.Column("seller_settings_id", sa.Uuid(), nullable=False),
        # Constant discriminator pinned to 1 (the singleton guard).
        sa.Column(
            "singleton",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("pickup_location", sa.Text(), nullable=True),
        sa.Column("upi_address", sa.Text(), nullable=True),
        sa.Column("upi_qr_object_key", sa.Text(), nullable=True),
        sa.Column(
            "utr_pattern",
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_UTR_PATTERN}'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        # Singleton guard mirroring meta's CHECK (meta_id = 1): the constant
        # discriminator is pinned to 1 and made UNIQUE so only one row exists.
        sa.CheckConstraint("singleton = 1", name="singleton"),
        # pickup_location is NULL until set; 1-500 chars when present.
        sa.CheckConstraint(
            "pickup_location IS NULL OR "
            "(length(pickup_location) >= 1 AND length(pickup_location) <= 500)",
            name="pickup_location_len",
        ),
        sa.PrimaryKeyConstraint("seller_settings_id", name="pk_seller_settings"),
        sa.UniqueConstraint("singleton", name="uq_seller_settings_singleton"),
    )

    # Seed the singleton row: NULL pickup/UPI values, default utr_pattern.
    # utr_pattern / updated_at / schema_version fall back to their server
    # defaults; only the PK and the singleton discriminator are supplied.
    op.execute(
        sa.text(
            "INSERT INTO seller_settings (seller_settings_id, singleton) "
            "VALUES (:sid, 1)"
        ).bindparams(sid=uuid.uuid4().hex)
    )


def downgrade() -> None:
    op.drop_table("seller_settings")
