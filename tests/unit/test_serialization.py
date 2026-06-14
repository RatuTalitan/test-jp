"""Unit tests for the versioned domain serialization layer (task 4.2).

Covers (design.md -> "Versioning Strategy (Req 13.7, 13.8)", Req 13.7):
  * round-trip of every domain entity is value-preserving;
  * ``Decimal`` money/quantities stay exact (serialized as strings, no float
    precision loss);
  * timezone-aware ``datetime`` values round-trip with their offset preserved;
  * every serialized payload carries a top-level ``format_version`` (and the
    order/seller-settings ``schema_version`` travels alongside it);
  * forward-compatible read: a payload with an unknown extra field still
    deserializes (the extra field is ignored), and an absent new optional field
    falls back to the entity default;
  * the notification ``payload`` and audit ``detail`` JSON documents carry the
    ``{format_version, ...}`` shape on both write and read.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from marketplace.domain import serialization as ser
from marketplace.domain.entities import (
    AuditAction,
    AuditEntry,
    Cart,
    CartItem,
    Category,
    Language,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
)

# A couple of fixed, timezone-aware instants (UTC and a +05:30 IST offset) so
# the tz handling is exercised with a non-UTC zone too.
UTC_NOW = datetime(2024, 3, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))
IST_NOW = datetime(2024, 3, 1, 18, 0, 45, 7, tzinfo=IST)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Entity builders: one fully-populated instance of every entity.
# ---------------------------------------------------------------------------
def _user() -> User:
    return User(
        user_id=_uid(),
        telegram_user_id=987654321,
        role=Role.CUSTOMER,
        verified_contact="+919812345678",
        contact_verified_at=UTC_NOW,
        offline_payment_allowed=True,
        language_preference=Language.HI,
        created_at=UTC_NOW,
        updated_at=IST_NOW,
    )


def _category() -> Category:
    return Category(category_id=_uid(), name="Cattle Feed", created_at=UTC_NOW)


def _product() -> Product:
    return Product(
        product_id=_uid(),
        name="Cotton Seed Oil Cake",
        category_id=_uid(),
        unit=Unit.QUINTAL,
        price_per_unit=Decimal("2599.99"),
        min_order_quantity=Decimal("0.500"),
        stock_quantity=Decimal("1234.125"),
        description="Premium grade",
        available=True,
        created_at=UTC_NOW,
        updated_at=IST_NOW,
    )


def _cart() -> Cart:
    return Cart(
        cart_id=_uid(),
        customer_id=_uid(),
        items=[
            CartItem(product_id=_uid(), quantity=Decimal("2.250"), cart_item_id=_uid()),
            CartItem(product_id=_uid(), quantity=Decimal("10")),
        ],
    )


def _order() -> Order:
    return Order(
        order_id=_uid(),
        customer_id=_uid(),
        state=OrderState.PAYMENT_SUBMITTED,
        total_amount=Decimal("5200.00"),
        items=[
            OrderItem(
                product_id=_uid(),
                ordered_quantity=Decimal("2.000"),
                unit_price=Decimal("2599.99"),
                line_amount=Decimal("5199.98"),
                order_item_id=_uid(),
            )
        ],
        order_number=42,
        rejection_reason=None,
        schema_version=1,
        created_at=UTC_NOW,
        updated_at=IST_NOW,
    )


def _payment() -> Payment:
    return Payment(
        payment_id=_uid(),
        order_id=_uid(),
        utr="ABCD12345678",
        utr_submitted_at=UTC_NOW,
        screenshot_object_key="payments/2024/abc.jpg",
        created_at=UTC_NOW,
        updated_at=IST_NOW,
    )


def _audit() -> AuditEntry:
    return AuditEntry(
        audit_id=_uid(),
        order_id=_uid(),
        action=AuditAction.MODIFY_LINE,
        detail={
            "format_version": ser.FORMAT_VERSION,
            "field": "ordered_quantity",
            "old_value": "2.000",
            "new_value": "3.000",
        },
        acting_user_id=_uid(),
        created_at=UTC_NOW,
    )


def _notification() -> Notification:
    return Notification(
        notification_id=_uid(),
        order_id=_uid(),
        recipient_id=_uid(),
        kind=NotificationKind.STATE_CHANGE,
        transition_seq=3,
        payload={
            "format_version": ser.FORMAT_VERSION,
            "new_state": "APPROVED",
            "order_number": 42,
        },
        status=NotificationStatus.PENDING,
        attempts=1,
        last_attempt_at=UTC_NOW,
        next_attempt_at=IST_NOW,
        created_at=UTC_NOW,
        updated_at=IST_NOW,
    )


def _seller_settings() -> SellerSettings:
    return SellerSettings(
        pickup_location="Main Mandi, Rajkot",
        upi_address="seller@upi",
        upi_qr_object_key="settings/qr.png",
        utr_pattern="^[A-Za-z0-9]{12}$",
        seller_settings_id=_uid(),
        updated_at=UTC_NOW,
        schema_version=1,
    )


ALL_ENTITIES = [
    ("User", User, _user),
    ("Category", Category, _category),
    ("Product", Product, _product),
    ("Cart", Cart, _cart),
    ("Order", Order, _order),
    ("Payment", Payment, _payment),
    ("AuditEntry", AuditEntry, _audit),
    ("Notification", Notification, _notification),
    ("SellerSettings", SellerSettings, _seller_settings),
]

# CartItem / OrderItem are also independently serializable (nested entities).
NESTED_ENTITIES = [
    ("CartItem", CartItem, lambda: CartItem(product_id=_uid(), quantity=Decimal("1.5"))),
    (
        "OrderItem",
        OrderItem,
        lambda: OrderItem(
            product_id=_uid(),
            ordered_quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            line_amount=Decimal("100.00"),
        ),
    ),
]


@pytest.mark.parametrize(
    "name, cls, build", ALL_ENTITIES + NESTED_ENTITIES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_round_trip_is_value_preserving(name, cls, build):
    """to_dict -> from_dict reproduces an equal entity for every type."""
    original = build()
    restored = ser.from_dict(cls, ser.to_dict(original))
    assert restored == original
    assert type(restored) is cls


@pytest.mark.parametrize(
    "name, cls, build", ALL_ENTITIES + NESTED_ENTITIES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_json_string_round_trip(name, cls, build):
    """dumps -> loads (a real JSON string) reproduces an equal entity."""
    original = build()
    restored = ser.loads(cls, ser.dumps(original))
    assert restored == original


@pytest.mark.parametrize(
    "name, cls, build", ALL_ENTITIES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_format_version_present_in_output(name, cls, build):
    """Every serialized entity carries the top-level format_version envelope."""
    data = ser.to_dict(build())
    assert data[ser.FORMAT_VERSION_KEY] == ser.FORMAT_VERSION


def test_order_carries_both_format_and_schema_version():
    """Order embeds the envelope format_version AND the row's schema_version."""
    data = ser.to_dict(_order())
    assert data["format_version"] == ser.FORMAT_VERSION
    assert data["schema_version"] == 1
    # schema_version survives the round-trip.
    assert ser.from_dict(Order, data).schema_version == 1


def test_seller_settings_carries_schema_version():
    data = ser.to_dict(_seller_settings())
    assert data["format_version"] == ser.FORMAT_VERSION
    assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# Decimal exactness (no float precision loss).
# ---------------------------------------------------------------------------
def test_decimal_serialized_as_string_and_stays_exact():
    """Money/quantities are emitted as strings and decoded back exactly."""
    p = _product()
    p.price_per_unit = Decimal("2599.99")
    p.stock_quantity = Decimal("1234.125")
    data = ser.to_dict(p)

    # Serialized as JSON strings, never floats.
    assert data["price_per_unit"] == "2599.99"
    assert data["stock_quantity"] == "1234.125"
    assert isinstance(data["price_per_unit"], str)

    restored = ser.from_dict(Product, data)
    assert restored.price_per_unit == Decimal("2599.99")
    assert restored.stock_quantity == Decimal("1234.125")
    # Trailing-zero scale is preserved (string form keeps it exact).
    assert str(restored.price_per_unit) == "2599.99"


def test_decimal_no_float_precision_loss_through_json():
    """A value unrepresentable as binary float survives a full JSON round-trip."""
    item = OrderItem(
        product_id=_uid(),
        ordered_quantity=Decimal("0.1"),
        unit_price=Decimal("0.2"),
        line_amount=Decimal("0.3"),
    )
    restored = ser.loads(OrderItem, ser.dumps(item))
    # 0.1 + 0.2 != 0.3 in binary float; here the exact decimals are preserved.
    assert restored.ordered_quantity + restored.unit_price == Decimal("0.3")
    assert restored.line_amount == Decimal("0.3")


# ---------------------------------------------------------------------------
# Timezone-aware datetime handling.
# ---------------------------------------------------------------------------
def test_timezone_aware_datetimes_preserved():
    """Aware datetimes round-trip with their offset (UTC and IST) intact."""
    u = _user()
    data = ser.to_dict(u)
    # ISO-8601 strings carry the offset.
    assert data["created_at"] == UTC_NOW.isoformat()
    assert data["updated_at"] == IST_NOW.isoformat()

    restored = ser.from_dict(User, data)
    assert restored.created_at == UTC_NOW
    assert restored.updated_at == IST_NOW
    # Same absolute instant AND same offset (tzinfo preserved, not normalised).
    assert restored.updated_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_uuid_and_enum_serialized_as_strings():
    """UUIDs serialize as strings and enums as their .value."""
    u = _user()
    data = ser.to_dict(u)
    assert data["user_id"] == str(u.user_id)
    assert data["role"] == "CUSTOMER"
    assert data["language_preference"] == "HI"
    restored = ser.from_dict(User, data)
    assert restored.user_id == u.user_id
    assert restored.role is Role.CUSTOMER
    assert restored.language_preference is Language.HI


def test_optional_none_fields_round_trip():
    """Unset optional fields serialize as null and decode back to None."""
    u = User(user_id=_uid(), telegram_user_id=1, role=Role.CUSTOMER)
    data = ser.to_dict(u)
    assert data["verified_contact"] is None
    assert data["contact_verified_at"] is None
    assert data["language_preference"] is None
    restored = ser.from_dict(User, data)
    assert restored == u
    assert restored.is_authenticated is False


# ---------------------------------------------------------------------------
# Forward-compatibility (additive-only read).
# ---------------------------------------------------------------------------
def test_unknown_extra_top_level_field_is_tolerated():
    """A payload with an unknown extra field still deserializes (ignored)."""
    data = ser.to_dict(_product())
    data["a_future_field"] = {"nested": [1, 2, 3]}
    data["another_new_flag"] = True
    restored = ser.from_dict(Product, data)
    assert isinstance(restored, Product)
    assert not hasattr(restored, "a_future_field")


def test_absent_new_optional_field_falls_back_to_default():
    """An older payload missing an optional field defers to the entity default."""
    data = ser.to_dict(_order())
    # Simulate an older payload that predates these optional fields.
    del data["rejection_reason"]
    del data["order_number"]
    del data["updated_at"]
    restored = ser.from_dict(Order, data)
    assert restored.rejection_reason is None
    assert restored.order_number is None
    assert restored.updated_at is None


def test_order_payload_without_schema_version_defaults_to_one():
    """A v1 order row lacking schema_version reads back as the default (1)."""
    data = ser.to_dict(_order())
    del data["schema_version"]
    assert ser.from_dict(Order, data).schema_version == 1


def test_unknown_extra_field_inside_nested_list_item_is_tolerated():
    """Extra fields on nested cart/order line items are ignored on read."""
    data = ser.to_dict(_cart())
    data["items"][0]["surprise"] = "ignored"
    restored = ser.from_dict(Cart, data)
    assert len(restored.items) == 2
    assert all(isinstance(i, CartItem) for i in restored.items)


# ---------------------------------------------------------------------------
# Versioned JSON documents: notification payload & audit detail.
# ---------------------------------------------------------------------------
def test_notification_payload_has_format_version_shape():
    """The serialized notification payload is a {format_version, ...} document."""
    data = ser.to_dict(_notification())
    payload = data["payload"]
    assert payload["format_version"] == ser.FORMAT_VERSION
    assert payload["new_state"] == "APPROVED"


def test_audit_detail_has_format_version_shape():
    """The serialized audit detail is a {format_version, ...} document."""
    data = ser.to_dict(_audit())
    detail = data["detail"]
    assert detail["format_version"] == ser.FORMAT_VERSION
    assert detail["field"] == "ordered_quantity"


def test_notification_payload_missing_version_gets_default_on_write():
    """A payload built without a version is normalised to carry format_version."""
    n = _notification()
    n.payload = {"new_state": "COMPLETED"}  # no format_version supplied
    data = ser.to_dict(n)
    assert data["payload"]["format_version"] == ser.FORMAT_VERSION
    assert data["payload"]["new_state"] == "COMPLETED"


def test_audit_detail_unknown_fields_preserved_with_version():
    """Unknown keys inside the audit detail document survive a round-trip."""
    a = _audit()
    a.detail = {"field": "x", "future_key": {"deep": 1}}
    restored = ser.from_dict(AuditEntry, ser.to_dict(a))
    assert restored.detail["future_key"] == {"deep": 1}
    assert restored.detail["format_version"] == ser.FORMAT_VERSION


def test_ensure_versioned_document_does_not_clobber_existing_version():
    """A document already declaring a (newer) version keeps it."""
    doc = ser.ensure_versioned_document({"format_version": 5, "x": 1})
    assert doc["format_version"] == 5
    assert ser.ensure_versioned_document(None) is None


# ---------------------------------------------------------------------------
# API guard rails.
# ---------------------------------------------------------------------------
def test_to_dict_rejects_non_entity():
    with pytest.raises(TypeError):
        ser.to_dict(object())


def test_from_dict_rejects_non_entity_class():
    with pytest.raises(TypeError):
        ser.from_dict(dict, {})


def test_dumps_emits_valid_json():
    """dumps() output is parseable JSON containing the envelope version."""
    raw = ser.dumps(_payment())
    parsed = json.loads(raw)
    assert parsed["format_version"] == ser.FORMAT_VERSION
    assert parsed["utr"] == "ABCD12345678"
