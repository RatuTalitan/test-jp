"""initial schema: meta, enums, users, categories, products

Task 3.1 - the first slice of the marketplace schema.

Creates:
  * the ``meta`` single-row table holding ``schema_version`` (Req 13.7);
  * the ``order_state``, ``unit``, ``role`` and ``language`` enum types
    (native ENUM on PostgreSQL; emitted as VARCHAR + CHECK on SQLite);
  * ``users`` with channel-neutral identity, role, the offline-payment flag,
    and the nullable ``language_preference`` (Req 1.6, 17.1, 18.2/18.4/18.5);
  * ``categories`` with a case-insensitive-unique name (Req 2.8, 2.11);
  * ``products`` with the range CHECK constraints (Req 2.5, 2.6, 2.9), the
    defense-in-depth non-negative stock guard, and the browsing indexes.

Carts/orders/payments/audit/notifications/seller_settings are added by later
migrations (tasks 3.2 / 3.3 / 3.5).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from marketplace.db.models import (
    LANGUAGE_ENUM_NAME,
    LANGUAGE_VALUES,
    ORDER_STATE_ENUM_NAME,
    ORDER_STATE_VALUES,
    PRICE_MAX,
    QUANTITY_MAX,
    ROLE_ENUM_NAME,
    ROLE_VALUES,
    UNIT_ENUM_NAME,
    UNIT_VALUES,
)

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(values: Sequence[str], name: str) -> sa.Enum:
    """Build a portable Enum type.

    ``create_constraint=True`` makes SQLite enforce the value set via a CHECK
    (PostgreSQL enforces via the native type). The type's lifecycle is left to
    the table create/drop ops: on PostgreSQL ``CREATE TABLE`` emits the
    ``CREATE TYPE`` and ``DROP TABLE`` emits the ``DROP TYPE``. Each enum below
    is referenced by exactly one table, so no duplicate type DDL is produced.
    """
    return sa.Enum(
        *values,
        name=name,
        native_enum=True,
        create_constraint=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"

    unit = _enum(UNIT_VALUES, UNIT_ENUM_NAME)
    role = _enum(ROLE_VALUES, ROLE_ENUM_NAME)
    language = _enum(LANGUAGE_VALUES, LANGUAGE_ENUM_NAME)

    # ``order_state`` is referenced by no column in this migration (the orders
    # table arrives in task 3.2), so on PostgreSQL we create the native type
    # explicitly now. ``create_type=False`` keeps the table ops from touching
    # it. On SQLite this is a no-op (enums are inline CHECKs on their columns).
    order_state = sa.Enum(
        *ORDER_STATE_VALUES,
        name=ORDER_STATE_ENUM_NAME,
        native_enum=True,
        create_type=False,
    )
    if is_postgresql:
        order_state.create(bind, checkfirst=True)

    # --- meta (single-row schema_version holder) ---------------------------
    op.create_table(
        "meta",
        sa.Column("meta_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("meta_id = 1", name="singleton"),
        sa.PrimaryKeyConstraint("meta_id", name="pk_meta"),
    )
    op.execute(sa.text("INSERT INTO meta (meta_id, schema_version) VALUES (1, 1)"))

    # --- users -------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("verified_contact", sa.Text(), nullable=True),
        sa.Column("contact_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", role, nullable=False),
        sa.Column(
            "offline_payment_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("language_preference", language, nullable=True),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
    )

    # --- categories --------------------------------------------------------
    op.create_table(
        "categories",
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(name) >= 1 AND length(name) <= 50", name="name_len"
        ),
        sa.PrimaryKeyConstraint("category_id", name="pk_categories"),
    )
    # Case-insensitive uniqueness: functional unique index on lower(name).
    op.create_index(
        "uq_categories_name_lower",
        "categories",
        [sa.text("lower(name)")],
        unique=True,
    )

    # --- products ----------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("unit", unit, nullable=False),
        sa.Column("price_per_unit", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_order_quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("stock_quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column(
            "available", sa.Boolean(), nullable=False, server_default=sa.true()
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
        sa.CheckConstraint(
            "length(name) >= 1 AND length(name) <= 100", name="name_len"
        ),
        sa.CheckConstraint(
            "description IS NULL OR length(description) <= 1000",
            name="description_len",
        ),
        sa.CheckConstraint(
            f"price_per_unit >= 0 AND price_per_unit <= {PRICE_MAX}",
            name="price_range",
        ),
        sa.CheckConstraint(
            f"min_order_quantity > 0 AND min_order_quantity <= {QUANTITY_MAX}",
            name="moq_range",
        ),
        sa.CheckConstraint(
            f"stock_quantity >= 0 AND stock_quantity <= {QUANTITY_MAX}",
            name="stock_range",
        ),
        # Defense-in-depth: stock can never be negative (task 3.1 / Req 7.4).
        sa.CheckConstraint("stock_quantity >= 0", name="stock_non_negative"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.category_id"],
            name="fk_products_category_id_categories",
        ),
        sa.PrimaryKeyConstraint("product_id", name="pk_products"),
    )
    op.create_index(
        "ix_products_category_id_available",
        "products",
        ["category_id", "available"],
        unique=False,
    )
    # Partial index over available products for fast browsing.
    op.create_index(
        "ix_products_available_partial",
        "products",
        ["category_id"],
        unique=False,
        postgresql_where=sa.text("available"),
        sqlite_where=sa.text("available"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"

    op.drop_index("ix_products_available_partial", table_name="products")
    op.drop_index("ix_products_category_id_available", table_name="products")
    # Dropping the table also drops its column-attached enum types on
    # PostgreSQL (unit, role, language).
    op.drop_table("products")

    op.drop_index("uq_categories_name_lower", table_name="categories")
    op.drop_table("categories")

    op.drop_table("users")
    op.drop_table("meta")

    # order_state has no column, so drop the standalone type explicitly.
    if is_postgresql:
        sa.Enum(name=ORDER_STATE_ENUM_NAME).drop(bind, checkfirst=True)
