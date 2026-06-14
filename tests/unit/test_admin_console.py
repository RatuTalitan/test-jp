"""Unit tests for the Admin_Console Seller settings (task 20.2).

Exercise the Admin_Console against the in-memory Unit-of-Work, a real
``AuthService`` (config-driven Seller gate), and the fake ObjectStore:

  * ``require_admin`` gating on every setter -- a non-Seller is rejected and the
    current value is left unchanged (Req 19.8);
  * Pickup_Location length validation -- empty/``None``/>500 rejected; 1 and 500
    accepted; an unset (NULL) Pickup_Location remains valid (Req 19.1/19.2);
  * UPI_Address VPA validation -- a valid ``local@handle`` accepted; missing
    ``@``, empty local, empty handle, and double ``@`` rejected (Req 19.3/19.4);
  * UPI_QR_Code -- supported image within the cap accepted (blob in object
    storage, **only** the key in the DB), unsupported format / oversized
    rejected at the boundary (Req 19.5/19.6);
  * ``Payment_Service.present_instructions`` reflects an updated ``upi_address`` /
    ``upi_qr_object_key`` written through the Admin_Console (Req 19.7);
  * the non-blocking "mark ready with no Pickup_Location" warning -- the order
    still transitions to READY_FOR_PICKUP while a warning flag is attached
    (Req 19.9).

Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.9.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.admin import (
    INVALID_VPA,
    MAX_QR_IMAGE_BYTES,
    PICKUP_LOCATION_INVALID,
    PICKUP_LOCATION_NOT_SET,
    QR_IMAGE_TOO_LARGE,
    UNSUPPORTED_QR_FORMAT,
    AdminConsole,
    ReadyResult,
)
from marketplace.auth.service import AuthService
from marketplace.domain.entities import (
    Order,
    OrderItem,
    OrderState,
    Role,
    SellerSettings,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import NotAuthorized, Rejected
from marketplace.payment import InMemoryObjectStore, PaymentService, UpiInstructions

SELLER_TELEGRAM_ID = 1
CUSTOMER_TELEGRAM_ID = 2

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32  # plausible tiny PNG blob


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def uow(db: InMemoryDatabase) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(db)


@pytest.fixture
def auth(db: InMemoryDatabase) -> AuthService:
    # The Seller gate is config-driven (role_of compares against the configured
    # SELLER_TELEGRAM_ID); the factory binds to the same in-memory store.
    return AuthService(lambda: InMemoryUnitOfWork(db), seller_telegram_id=SELLER_TELEGRAM_ID)


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def console(
    uow: InMemoryUnitOfWork, auth: AuthService, store: InMemoryObjectStore
) -> AdminConsole:
    return AdminConsole(uow, auth, object_store=store)


def make_seller(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=SELLER_TELEGRAM_ID,
            role=Role.ADMIN,
            verified_contact="+910000000000",
            contact_verified_at=datetime(2024, 1, 1),
        )
    )


def make_customer(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=CUSTOMER_TELEGRAM_ID,
            role=Role.CUSTOMER,
            verified_contact="+919999999999",
            contact_verified_at=datetime(2024, 1, 2),
        )
    )


def seed_settings(uow: InMemoryUnitOfWork, **kwargs) -> SellerSettings:
    base = dict(
        seller_settings_id=new_id(),
        pickup_location="Mandi Road, Rajkot",
        upi_address="seller@upi",
        upi_qr_object_key="upi-qr/existing.png",
    )
    base.update(kwargs)
    return uow.seller_settings.upsert(SellerSettings(**base))


# --------------------------------------------------------------------------- #
# require_admin gating (Req 19.8) -- non-Seller leaves the value unchanged
# --------------------------------------------------------------------------- #
def test_set_pickup_location_rejects_non_seller_and_leaves_value(console, uow):
    make_customer(uow)
    seed_settings(uow, pickup_location="OLD ADDRESS")

    result = console.set_pickup_location("NEW ADDRESS", CUSTOMER_TELEGRAM_ID)

    assert isinstance(result, NotAuthorized)
    assert uow.seller_settings.get().pickup_location == "OLD ADDRESS"


def test_set_upi_address_rejects_non_seller_and_leaves_value(console, uow):
    make_customer(uow)
    seed_settings(uow, upi_address="old@upi")

    result = console.set_upi_address("new@upi", CUSTOMER_TELEGRAM_ID)

    assert isinstance(result, NotAuthorized)
    assert uow.seller_settings.get().upi_address == "old@upi"


def test_set_upi_qr_rejects_non_seller_and_leaves_value(console, uow, store):
    make_customer(uow)
    seed_settings(uow, upi_qr_object_key="upi-qr/old.png")

    result = console.set_upi_qr(PNG, "image/png", len(PNG), CUSTOMER_TELEGRAM_ID)

    assert isinstance(result, NotAuthorized)
    assert uow.seller_settings.get().upi_qr_object_key == "upi-qr/old.png"
    assert len(store) == 0  # nothing stored on a rejected upload


# --------------------------------------------------------------------------- #
# Pickup_Location validation (Req 19.1 / 19.2)
# --------------------------------------------------------------------------- #
def test_set_pickup_location_persists_valid_value(console, uow):
    make_seller(uow)

    result = console.set_pickup_location("Shop 4, Main Bazaar", SELLER_TELEGRAM_ID)

    assert isinstance(result, SellerSettings)
    assert result.pickup_location == "Shop 4, Main Bazaar"
    assert uow.seller_settings.get().pickup_location == "Shop 4, Main Bazaar"


@pytest.mark.parametrize("length", [1, 500])
def test_set_pickup_location_accepts_boundary_lengths(console, uow, length):
    make_seller(uow)
    text = "x" * length

    result = console.set_pickup_location(text, SELLER_TELEGRAM_ID)

    assert not isinstance(result, (Rejected, NotAuthorized))
    assert uow.seller_settings.get().pickup_location == text


@pytest.mark.parametrize("text", ["", "y" * 501, None])
def test_set_pickup_location_rejects_empty_or_too_long(console, uow, text):
    make_seller(uow)
    seed_settings(uow, pickup_location="KEEP ME")

    result = console.set_pickup_location(text, SELLER_TELEGRAM_ID)

    assert isinstance(result, Rejected)
    assert result.code == PICKUP_LOCATION_INVALID
    # Current value left unchanged on a rejected submission (Req 19.2).
    assert uow.seller_settings.get().pickup_location == "KEEP ME"


def test_unset_pickup_location_stays_valid(console, uow):
    """An unset (NULL) Pickup_Location is valid -- nothing forces it to be set."""
    make_seller(uow)
    # Seed everything EXCEPT pickup_location.
    uow.seller_settings.upsert(
        SellerSettings(seller_settings_id=new_id(), upi_address="seller@upi")
    )

    # Setting only the UPI address must not invent a Pickup_Location.
    result = console.set_upi_address("seller2@okhdfc", SELLER_TELEGRAM_ID)

    assert result.pickup_location is None
    assert uow.seller_settings.get().pickup_location is None


# --------------------------------------------------------------------------- #
# UPI_Address VPA validation (Req 19.3 / 19.4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("vpa", ["seller@upi", "a@b", "9876543210@okhdfc", "name.surname@ybl"])
def test_set_upi_address_accepts_valid_vpa(console, uow, vpa):
    make_seller(uow)

    result = console.set_upi_address(vpa, SELLER_TELEGRAM_ID)

    assert not isinstance(result, (Rejected, NotAuthorized))
    assert uow.seller_settings.get().upi_address == vpa


@pytest.mark.parametrize(
    "vpa",
    [
        "no-at-symbol",  # missing @
        "@handle",       # empty local part
        "local@",        # empty handle
        "a@b@c",         # more than one @
        "",              # empty string
    ],
)
def test_set_upi_address_rejects_malformed_vpa(console, uow, vpa):
    make_seller(uow)
    seed_settings(uow, upi_address="keep@upi")

    result = console.set_upi_address(vpa, SELLER_TELEGRAM_ID)

    assert isinstance(result, Rejected)
    assert result.code == INVALID_VPA
    # Current value left unchanged on a rejected submission (Req 19.4).
    assert uow.seller_settings.get().upi_address == "keep@upi"


# --------------------------------------------------------------------------- #
# UPI_QR_Code validation + store-only-the-key (Req 19.5 / 19.6)
# --------------------------------------------------------------------------- #
def test_set_upi_qr_stores_blob_and_only_key_in_db(console, uow, store):
    make_seller(uow)

    result = console.set_upi_qr(PNG, "image/png", len(PNG), SELLER_TELEGRAM_ID)

    key = uow.seller_settings.get().upi_qr_object_key
    assert result.upi_qr_object_key == key
    # The blob went to object storage and only its key reached the DB.
    assert key in store
    assert store.get(key).data == PNG
    assert store.get(key).content_type == "image/png"
    assert isinstance(key, str)


def test_set_upi_qr_accepts_exactly_max_size(console, uow, store):
    make_seller(uow)

    # The boundary itself (exactly 10 MB) is allowed; ``size`` is the declared
    # length so we needn't materialize 10 MB of bytes.
    result = console.set_upi_qr(PNG, "image/jpeg", MAX_QR_IMAGE_BYTES, SELLER_TELEGRAM_ID)

    assert not isinstance(result, (Rejected, NotAuthorized))
    assert len(store) == 1


def test_set_upi_qr_rejects_unsupported_format_and_leaves_value(console, uow, store):
    make_seller(uow)
    seed_settings(uow, upi_qr_object_key="upi-qr/old.png")

    result = console.set_upi_qr(b"GIF89a", "image/gif", 6, SELLER_TELEGRAM_ID)

    assert isinstance(result, Rejected)
    assert result.code == UNSUPPORTED_QR_FORMAT
    assert uow.seller_settings.get().upi_qr_object_key == "upi-qr/old.png"
    assert len(store) == 0  # nothing stored on a rejected upload


def test_set_upi_qr_rejects_oversized_and_leaves_value(console, uow, store):
    make_seller(uow)
    seed_settings(uow, upi_qr_object_key="upi-qr/old.png")

    result = console.set_upi_qr(PNG, "image/png", MAX_QR_IMAGE_BYTES + 1, SELLER_TELEGRAM_ID)

    assert isinstance(result, Rejected)
    assert result.code == QR_IMAGE_TOO_LARGE
    assert uow.seller_settings.get().upi_qr_object_key == "upi-qr/old.png"
    assert len(store) == 0


def test_set_upi_qr_accepts_user_object_as_actor(console, uow, store):
    """The setter accepts a User (not just a Telegram id) as ``acting_user``."""
    seller = make_seller(uow)

    result = console.set_upi_qr(PNG, "image/webp", len(PNG), seller)

    assert not isinstance(result, (Rejected, NotAuthorized))
    assert uow.seller_settings.get().upi_qr_object_key == result.upi_qr_object_key


# --------------------------------------------------------------------------- #
# present_instructions reflects updated settings (Req 19.7)
# --------------------------------------------------------------------------- #
def test_present_instructions_reflects_updated_seller_settings(console, uow, store):
    make_seller(uow)
    customer = make_customer(uow)

    order = uow.orders.add(
        Order(
            order_id=new_id(),
            customer_id=customer.user_id,
            state=OrderState.PAYMENT_PENDING,
            total_amount=Decimal("250.00"),
            items=[
                OrderItem(
                    product_id=new_id(),
                    ordered_quantity=Decimal("5"),
                    unit_price=Decimal("50.00"),
                    line_amount=Decimal("250.00"),
                )
            ],
            created_at=datetime(2024, 5, 1),
        )
    )

    # Configure address + QR through the Admin_Console, then read them back via
    # the Payment_Service (which reads the same seller_settings row).
    console.set_upi_address("seller@okaxis", SELLER_TELEGRAM_ID)
    console.set_upi_qr(PNG, "image/png", len(PNG), SELLER_TELEGRAM_ID)
    expected_key = uow.seller_settings.get().upi_qr_object_key

    payment = PaymentService(uow, object_store=store)
    instructions = payment.present_instructions(order.order_id)

    assert isinstance(instructions, UpiInstructions)
    assert instructions.upi_address == "seller@okaxis"
    assert instructions.upi_qr_object_key == expected_key
    assert instructions.amount_due == Decimal("250.00")

    # An UPDATE flows through too: change the address and re-read.
    console.set_upi_address("seller@upi", SELLER_TELEGRAM_ID)
    updated = payment.present_instructions(order.order_id)
    assert updated.upi_address == "seller@upi"


# --------------------------------------------------------------------------- #
# Non-blocking mark-ready warning when no Pickup_Location is set (Req 19.9)
# --------------------------------------------------------------------------- #
def _approved_order(uow: InMemoryUnitOfWork, customer: User) -> Order:
    return uow.orders.add(
        Order(
            order_id=new_id(),
            customer_id=customer.user_id,
            state=OrderState.APPROVED,
            total_amount=Decimal("100.00"),
            items=[
                OrderItem(
                    product_id=new_id(),
                    ordered_quantity=Decimal("2"),
                    unit_price=Decimal("50.00"),
                    line_amount=Decimal("100.00"),
                )
            ],
            created_at=datetime(2024, 5, 2),
        )
    )


def test_mark_ready_warns_when_no_pickup_location_but_still_transitions(console, uow):
    seller = make_seller(uow)
    customer = make_customer(uow)
    order = _approved_order(uow, customer)
    # No seller_settings row at all -> no Pickup_Location configured.

    result = console.mark_ready(order.order_id, seller)

    assert isinstance(result, ReadyResult)
    assert result.pickup_location_warning is True
    assert result.warning_code == PICKUP_LOCATION_NOT_SET
    # The transition STILL happened (warning is non-blocking, Req 19.9).
    assert result.order.state is OrderState.READY_FOR_PICKUP
    assert uow.orders.get(order.order_id).state is OrderState.READY_FOR_PICKUP


def test_mark_ready_no_warning_when_pickup_location_set(console, uow):
    seller = make_seller(uow)
    customer = make_customer(uow)
    seed_settings(uow, pickup_location="Shop 4, Main Bazaar")
    order = _approved_order(uow, customer)

    result = console.mark_ready(order.order_id, seller)

    assert isinstance(result, ReadyResult)
    assert result.pickup_location_warning is False
    assert result.warning_code is None
    assert result.order.state is OrderState.READY_FOR_PICKUP


def test_mark_ready_rejects_non_seller(console, uow):
    customer = make_customer(uow)
    order = _approved_order(uow, customer)

    result = console.mark_ready(order.order_id, CUSTOMER_TELEGRAM_ID)

    assert isinstance(result, NotAuthorized)
    # State unchanged on a rejected (gated) action.
    assert uow.orders.get(order.order_id).state is OrderState.APPROVED


def test_mark_ready_propagates_wrong_state_failure(console, uow):
    """A wrong starting state is a state-preserving failure, not a ReadyResult."""
    seller = make_seller(uow)
    customer = make_customer(uow)
    # PAYMENT_PENDING has no MARK_READY edge.
    order = uow.orders.add(
        Order(
            order_id=new_id(),
            customer_id=customer.user_id,
            state=OrderState.PAYMENT_PENDING,
            total_amount=Decimal("10.00"),
            items=[],
            created_at=datetime(2024, 5, 3),
        )
    )

    result = console.mark_ready(order.order_id, seller)

    assert not isinstance(result, ReadyResult)
    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING
