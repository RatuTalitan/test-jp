"""Catalog_Service subsystem.

CRUD for Categories and Products, field validation, availability handling, and
Customer-facing browsing. Language-agnostic: returns typed results and stable
status/error codes (never localized prose).
"""

from marketplace.catalog.service import (
    CATEGORY_NAME_MAX_LEN,
    CATEGORY_NAME_MIN_LEN,
    DESCRIPTION_MAX_LEN,
    IN_STOCK,
    MOQ_MAX,
    NAME_MAX_LEN,
    NAME_MIN_LEN,
    OUT_OF_STOCK,
    PRICE_MAX,
    PRICE_MIN,
    STOCK_MAX,
    STOCK_MIN,
    CatalogService,
    EmptyCatalog,
    EmptyCategory,
    ProductDetail,
)

__all__ = [
    "CatalogService",
    "ProductDetail",
    "EmptyCatalog",
    "EmptyCategory",
    "IN_STOCK",
    "OUT_OF_STOCK",
    "NAME_MIN_LEN",
    "NAME_MAX_LEN",
    "DESCRIPTION_MAX_LEN",
    "CATEGORY_NAME_MIN_LEN",
    "CATEGORY_NAME_MAX_LEN",
    "PRICE_MIN",
    "PRICE_MAX",
    "MOQ_MAX",
    "STOCK_MIN",
    "STOCK_MAX",
]
