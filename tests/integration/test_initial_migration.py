"""Integration test for the initial schema migration (task 3.1).

Applies the Alembic migration to a fresh database and asserts that the
expected tables, the ``meta.schema_version`` seed row, and the key constraints
(range CHECKs, the defense-in-depth non-negative stock guard, case-insensitive
category uniqueness, ``telegram_user_id`` uniqueness, enum value sets, and the
column defaults) are present and enforced.

The migration is run exactly the way the application/CI would run it - through
the Alembic command API against the ``DB_URL`` the migration environment reads -
so this also exercises ``migrations/env.py`` URL resolution.

The suite always runs against a throwaway **SQLite** database (local/dev/test
backend). It additionally runs against **PostgreSQL** when the
``MARKETPLACE_TEST_PG_URL`` environment variable points at a reachable server,
covering the native-enum runtime backend.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {"meta", "users", "categories", "products"}


def _alembic_config(db_url: str) -> Config:
    """Build an Alembic config pointed at this project's migrations + db_url."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _new_category(conn: sa.Connection, name: str) -> str:
    cid = uuid.uuid4().hex
    conn.execute(
        sa.text("INSERT INTO categories (category_id, name) VALUES (:id, :name)"),
        {"id": cid, "name": name},
    )
    return cid


def _insert_product(conn: sa.Connection, category_id: str, **overrides) -> None:
    params = {
        "id": uuid.uuid4().hex,
        "name": "Cotton Seed Oil Cake",
        "category_id": category_id,
        "description": "Premium grade",
        "unit": "KILOGRAM",
        "price": "100.00",
        "moq": "10.000",
        "stock": "500.000",
    }
    params.update(overrides)
    conn.execute(
        sa.text(
            "INSERT INTO products (product_id, name, category_id, description, "
            "unit, price_per_unit, min_order_quantity, stock_quantity) "
            "VALUES (:id, :name, :category_id, :description, :unit, :price, "
            ":moq, :stock)"
        ),
        params,
    )


def _insert_user(conn: sa.Connection, telegram_user_id: int, **overrides) -> None:
    params = {
        "id": uuid.uuid4().hex,
        "tg": telegram_user_id,
        "role": "CUSTOMER",
    }
    params.update(overrides)
    conn.execute(
        sa.text(
            "INSERT INTO users (user_id, telegram_user_id, role) "
            "VALUES (:id, :tg, :role)"
        ),
        params,
    )


# ---------------------------------------------------------------------------
# Backend fixtures
# ---------------------------------------------------------------------------
def _backends():
    """Yield pytest params for every database backend under test."""
    params = [pytest.param("sqlite", id="sqlite")]
    if os.environ.get("MARKETPLACE_TEST_PG_URL"):
        params.append(pytest.param("postgresql", id="postgresql"))
    return params


@pytest.fixture(params=_backends())
def engine(request, tmp_path, monkeypatch) -> sa.Engine:
    """Apply the migration to a fresh DB and yield an engine bound to it."""
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'marketplace_test.db'}"
    else:
        url = os.environ["MARKETPLACE_TEST_PG_URL"]

    # env.py reads DB_URL (the config-loader variable) when no -x override given.
    monkeypatch.setenv("DB_URL", url)
    cfg = _alembic_config(url)

    # Start from a clean slate (important for a reused PostgreSQL database).
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = sa.create_engine(url)
    try:
        yield eng
    finally:
        eng.dispose()
        command.downgrade(cfg, "base")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_expected_tables_exist(engine: sa.Engine):
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES.issubset(tables), tables


def test_schema_version_seed_row(engine: sa.Engine):
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT meta_id, schema_version FROM meta")).all()
    assert rows == [(1, 1)]


def test_meta_is_singleton(engine: sa.Engine):
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text("INSERT INTO meta (meta_id, schema_version) VALUES (2, 1)")
            )


def test_valid_rows_insert_successfully(engine: sa.Engine):
    with engine.begin() as conn:
        cid = _new_category(conn, "Oil Cakes")
        _insert_product(conn, cid)
        _insert_user(conn, telegram_user_id=111)


# ---------------------------------------------------------------------------
# Column defaults
# ---------------------------------------------------------------------------
def test_user_column_defaults(engine: sa.Engine):
    with engine.begin() as conn:
        _insert_user(conn, telegram_user_id=222)
        row = conn.execute(
            sa.text(
                "SELECT offline_payment_allowed, language_preference "
                "FROM users WHERE telegram_user_id = 222"
            )
        ).one()
    offline_allowed, language_preference = row
    # offline_payment_allowed defaults to false; language_preference is NULL
    # (NULL/unset -> Hindi default applied by the renderer, Req 18.2/18.4).
    assert bool(offline_allowed) is False
    assert language_preference is None


def test_product_available_defaults_true(engine: sa.Engine):
    with engine.begin() as conn:
        cid = _new_category(conn, "Defaults Cat")
        pid = uuid.uuid4().hex
        conn.execute(
            sa.text(
                "INSERT INTO products (product_id, name, category_id, unit, "
                "price_per_unit, min_order_quantity, stock_quantity) "
                "VALUES (:id, :n, :c, 'BAG', 50, 1, 0)"
            ),
            {"id": pid, "n": "Defaulted", "c": cid},
        )
        available = conn.execute(
            sa.text("SELECT available FROM products WHERE product_id = :id"),
            {"id": pid},
        ).scalar_one()
    assert bool(available) is True


def test_zero_stock_product_is_allowed(engine: sa.Engine):
    # Stock == 0 must be a valid product (Req 2.9 lower bound is inclusive).
    with engine.begin() as conn:
        cid = _new_category(conn, "Zero Stock Cat")
        _insert_product(conn, cid, stock="0.000")


# ---------------------------------------------------------------------------
# Constraint enforcement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"stock": "-1.000"}, id="negative-stock"),
        pytest.param({"stock": "10000000.000"}, id="stock-above-max"),
        pytest.param({"price": "-0.01"}, id="negative-price"),
        pytest.param({"price": "10000000.00"}, id="price-above-max"),
        pytest.param({"moq": "0.000"}, id="moq-zero"),
        pytest.param({"moq": "-5.000"}, id="moq-negative"),
        pytest.param({"moq": "10000000.000"}, id="moq-above-max"),
        pytest.param({"unit": "TONNE"}, id="invalid-unit-enum"),
    ],
)
def test_product_constraints_reject_out_of_range(engine: sa.Engine, overrides):
    with engine.begin() as conn:
        cid = _new_category(conn, f"Cat {uuid.uuid4().hex[:8]}")
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _insert_product(conn, cid, **overrides)


@pytest.mark.parametrize(
    "bad_name",
    [
        pytest.param("", id="empty-name"),
        pytest.param("x" * 51, id="name-too-long"),
    ],
)
def test_category_name_length_constraint(engine: sa.Engine, bad_name):
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _new_category(conn, bad_name)


def test_category_name_case_insensitive_unique(engine: sa.Engine):
    with engine.begin() as conn:
        _new_category(conn, "Seeds")
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _new_category(conn, "SEEDS")


def test_product_name_length_constraints(engine: sa.Engine):
    with engine.begin() as conn:
        cid = _new_category(conn, "Name Cat")
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _insert_product(conn, cid, name="")
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _insert_product(conn, cid, name="x" * 101)


def test_telegram_user_id_unique(engine: sa.Engine):
    with engine.begin() as conn:
        _insert_user(conn, telegram_user_id=999)
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _insert_user(conn, telegram_user_id=999)


@pytest.mark.parametrize(
    "column,value",
    [
        pytest.param("role", "SUPERUSER", id="invalid-role"),
        pytest.param("language_preference", "FR", id="invalid-language"),
    ],
)
def test_user_enum_constraints(engine: sa.Engine, column, value):
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    f"INSERT INTO users (user_id, telegram_user_id, role, "
                    f"{column}) VALUES (:id, :tg, :role, :val)"
                    if column != "role"
                    else "INSERT INTO users (user_id, telegram_user_id, role) "
                    "VALUES (:id, :tg, :val)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "tg": 12345,
                    "role": "CUSTOMER",
                    "val": value,
                },
            )


def test_downgrade_removes_all_tables(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'downgrade_test.db'}"
    monkeypatch.setenv("DB_URL", url)
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    eng = sa.create_engine(url)
    try:
        remaining = set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()
    assert not (EXPECTED_TABLES & remaining), remaining
