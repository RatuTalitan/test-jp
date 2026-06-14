"""carts, cart_items, orders, order_items

Task 3.2 - the second slice of the marketplace schema (design.md -> Data Models
-> Carts/Cart_Items and Orders/Order_Items).

Creates:
  * ``carts``       - one active cart per customer (UNIQUE ``customer_id``);
  * ``cart_items``  - per-cart product lines with ``UNIQUE(cart_id, product_id)``
                      enforcing combine-on-add (Req 4.5);
  * ``orders``      - placed orders with a human-friendly serial ``order_number``
                      (UNIQUE), the lifecycle ``state`` (reusing the ``order_state``
                      enum created in migration 0001), ``total_amount``, a length-
                      bounded ``rejection_reason`` (<=500, Req 7.5), timestamps and
                      ``schema_version`` (Req 13.7), plus the ``(customer_id,
                      created_at DESC)`` and ``(state, created_at)`` indexes
                      (Req 10.1/10.6);
  * ``order_items`` - per-order product lines with a snapshot ``unit_price`` and
                      ``line_amount`` (Req 5.2) and a ``(product_id)`` index for
                      fulfillability recompute (Req 16.1).

The ``order_state`` enum type is **reused** from migration 0001 and is NOT
recreated here (``create_type=False`` on PostgreSQL; on SQLite the enum is an
inline CHECK on the ``orders.state`` column).

Revision ID: 0002_carts_orders
Revises: 0001_initial_schema
Create Date: 2024-01-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from marketplace.db.models import ORDER_STATE_ENUM_NAME, ORDER_STATE_VALUES

# revision identifiers, used by Alembic.
revision: str = "0002_carts_orders"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reuse the ``order_state`` type created in 0001 - do not recreate it.
    order_state = sa.Enum(
        *ORDER_STATE_VALUES,
        name=ORDER_STATE_ENUM_NAME,
        native_enum=True,
        create_constraint=True,  # inline CHECK on SQLite
        create_type=False,  # type already exists on PostgreSQL (from 0001)
    )

    # --- carts (one active cart per customer) ------------------------------
    op.create_table(
        "carts",
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["users.user_id"],
            name="fk_carts_customer_id_users",
        ),
        sa.PrimaryKeyConstraint("cart_id", name="pk_carts"),
        sa.UniqueConstraint("customer_id", name="uq_carts_customer_id"),
    )

    # --- cart_items (combine-on-add via UNIQUE(cart_id, product_id)) -------
    op.create_table(
        "cart_items",
        sa.Column("cart_item_id", sa.Uuid(), nullable=False),
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.ForeignKeyConstraint(
            ["cart_id"],
            ["carts.cart_id"],
            name="fk_cart_items_cart_id_carts",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            name="fk_cart_items_product_id_products",
        ),
        sa.PrimaryKeyConstraint("cart_item_id", name="pk_cart_items"),
        sa.UniqueConstraint(
            "cart_id", "product_id", name="uq_cart_items_cart_id_product_id"
        ),
    )

    # --- orders ------------------------------------------------------------
    op.create_table(
        "orders",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        # Human-friendly serial id (BIGINT IDENTITY on PostgreSQL).
        sa.Column("order_number", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("state", order_state, nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
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
            "rejection_reason IS NULL OR length(rejection_reason) <= 500",
            name="rejection_reason_len",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["users.user_id"],
            name="fk_orders_customer_id_users",
        ),
        sa.PrimaryKeyConstraint("order_id", name="pk_orders"),
        sa.UniqueConstraint("order_number", name="uq_orders_order_number"),
    )
    # History listing: newest-first per customer (Req 10.1).
    op.create_index(
        "ix_orders_customer_id_created_at",
        "orders",
        ["customer_id", sa.text("created_at DESC")],
        unique=False,
    )
    # Active-order listing by state then time (Req 10.6).
    op.create_index(
        "ix_orders_state_created_at",
        "orders",
        ["state", "created_at"],
        unique=False,
    )

    # --- order_items (price snapshot per line) -----------------------------
    op.create_table(
        "order_items",
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("ordered_quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("line_amount", sa.Numeric(12, 2), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
            name="fk_order_items_order_id_orders",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            name="fk_order_items_product_id_products",
        ),
        sa.PrimaryKeyConstraint("order_item_id", name="pk_order_items"),
    )
    # Fulfillability recompute scans order lines by product (Req 16.1).
    op.create_index(
        "ix_order_items_product_id",
        "order_items",
        ["product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_order_items_product_id", table_name="order_items")
    op.drop_table("order_items")

    op.drop_index("ix_orders_state_created_at", table_name="orders")
    op.drop_index("ix_orders_customer_id_created_at", table_name="orders")
    # Dropping ``orders`` does NOT drop the ``order_state`` type (it was created
    # standalone in 0001 with create_type=False here), so the type survives for
    # 0001's downgrade to drop.
    op.drop_table("orders")

    op.drop_table("cart_items")
    op.drop_table("carts")
