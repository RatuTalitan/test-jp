"""Unit tests for PaymentService (tasks 11.1 / 11.2).

Exercise the Payment_Service against the in-memory Unit-of-Work and a fake
ObjectStore:

  * ``present_instructions`` reads the Seller-configured UPI details + amount due
    (Req 6.1/19.7) and rejects when nothing is configured / the order is missing;
  * ``submit_utr`` happy path records the UTR, transitions PAYMENT_PENDING ->
    PAYMENT_SUBMITTED, and enqueues the seller verification notification
    (Req 6.2/6.5);
  * wrong-state submission is rejected leaving state unchanged (Req 6.7);
  * invalid format is rejected against both the default and a customized
    ``utr_pattern`` (Req 6.2/6.6);
  * a duplicate UTR against a different order is a conflict (Req 6.4);
  * ``attach_screenshot`` accepts supported formats within the size cap and
    rejects unsupported formats / oversized blobs at the boundary, storing only
    the object key in the DB (Req 6.3/6.8).

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 19.7.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.domain.entities import (
    DEFAULT_UTR_PATTERN,
    NotificationKind,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Role,
    SellerSettings,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import Conflict, NotFound, Rejected
from marketplace.order.state_machine import Actor
from marketplace.payment import (
    DUPLICATE_UTR,
    IMAGE_TOO_LARGE,
    INVALID_UTR_FORMAT,
    MAX_SCREENSHOT_BYTES,
    PAYMENT_NOT_PENDING,
    PAYMENT_SUBMITTED_TRANSITION_SEQ,
    UNSUPPORTED_IMAGE_FORMAT,
    UPI_NOT_CONFIGURED,
    InMemoryObjectStore,
    PaymentService,
    UpiInstructions,
)

VALID_UTR = "ABC123def456"  # exactly 12 alphanumeric -> matches the default


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
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def service(uow: InMemoryUnitOfWork, store: InMemoryObjectStore) -> PaymentService:
    return PaymentService(uow, object_store=store)


def make_seller(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=1,
            role=Role.ADMIN,
            verified_contact="+910000000000",
            contact_verified_at=datetime(2024, 1, 1),
        )
    )


def make_customer(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=2,
            role=Role.CUSTOMER,
            verified_contact="+919999999999",
            contact_verified_at=datetime(2024, 1, 2),
        )
    )


def seed_settings(
    uow: InMemoryUnitOfWork,
    *,
    upi_address: str | None = "seller@upi",
    upi_qr_object_key: str | None = "seller-qr/qr.png",
    utr_pattern: str = DEFAULT_UTR_PATTERN,
) -> SellerSettings:
    return uow.seller_settings.upsert(
        SellerSettings(
            seller_settings_id=new_id(),
            pickup_location="Mandi Road, Rajkot",
            upi_address=upi_address,
            upi_qr_object_key=upi_qr_object_key,
            utr_pattern=utr_pattern,
        )
    )


def make_order(
    uow: InMemoryUnitOfWork,
    customer_id,
    *,
    state: OrderState = OrderState.PAYMENT_PENDING,
    total: str = "210.00",
) -> Order:
    order = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=state,
        total_amount=Decimal(total),
        items=[
            OrderItem(
                product_id=new_id(),
                ordered_quantity=Decimal("2"),
                unit_price=Decimal("105.00"),
                line_amount=Decimal("210.00"),
            )
        ],
        created_at=datetime(2024, 1, 3),
    )
    return uow.orders.add(order)


# --------------------------------------------------------------------------- #
# present_instructions (Req 6.1, 19.7)
# --------------------------------------------------------------------------- #
def test_present_instructions_reads_seller_settings(uow, service):
    customer = make_customer(uow)
    seed_settings(uow, upi_address="seller@okhdfc", upi_qr_object_key="qr/abc.png")
    order = make_order(uow, customer.user_id, total="210.00")

    result = service.present_instructions(order.order_id)

    assert isinstance(result, UpiInstructions)
    assert result.upi_address == "seller@okhdfc"
    assert result.upi_qr_object_key == "qr/abc.png"
    assert result.amount_due == Decimal("210.00")
    assert result.order_id == order.order_id


def test_present_instructions_missing_order_is_not_found(uow, service):
    seed_settings(uow)
    result = service.present_instructions(new_id())
    assert isinstance(result, NotFound)
    assert result.entity == "order"


def test_present_instructions_without_settings_is_rejected(uow, service):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)  # no seller_settings seeded
    result = service.present_instructions(order.order_id)
    assert isinstance(result, Rejected)
    assert result.code == UPI_NOT_CONFIGURED


def test_present_instructions_without_upi_address_is_rejected(uow, service):
    customer = make_customer(uow)
    seed_settings(uow, upi_address=None)
    order = make_order(uow, customer.user_id)
    result = service.present_instructions(order.order_id)
    assert isinstance(result, Rejected)
    assert result.code == UPI_NOT_CONFIGURED


# --------------------------------------------------------------------------- #
# submit_utr happy path (Req 6.2, 6.5)
# --------------------------------------------------------------------------- #
def test_submit_utr_happy_path_transitions_and_records(uow, service):
    seller = make_seller(uow)
    customer = make_customer(uow)
    seed_settings(uow)
    order = make_order(uow, customer.user_id)

    result = service.submit_utr(
        order.order_id, VALID_UTR, Actor.customer(customer.user_id)
    )

    # Transitioned PAYMENT_PENDING -> PAYMENT_SUBMITTED.
    assert isinstance(result, Order)
    assert result.state is OrderState.PAYMENT_SUBMITTED
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_SUBMITTED

    # UTR recorded on the order's Payment row with a submission timestamp.
    payment = uow.payments.get_by_order(order.order_id)
    assert payment is not None
    assert payment.utr == VALID_UTR
    assert payment.utr_submitted_at is not None
    # Looking up by UTR finds this order's payment (uniqueness index).
    assert uow.payments.get_by_utr(VALID_UTR).order_id == order.order_id


def test_submit_utr_enqueues_seller_verification_notification(uow, service):
    seller = make_seller(uow)
    customer = make_customer(uow)
    seed_settings(uow)
    order = make_order(uow, customer.user_id)

    service.submit_utr(order.order_id, VALID_UTR, Actor.customer(customer.user_id))

    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        PAYMENT_SUBMITTED_TRANSITION_SEQ,
    )
    assert notif is not None
    assert notif.recipient_id == seller.user_id
    assert notif.payload["new_state"] == OrderState.PAYMENT_SUBMITTED.value


def test_submit_utr_missing_order_is_not_found(uow, service):
    customer = make_customer(uow)
    result = service.submit_utr(new_id(), VALID_UTR, Actor.customer(customer.user_id))
    assert isinstance(result, NotFound)


# --------------------------------------------------------------------------- #
# Wrong-state submission (Req 6.7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [
        OrderState.PAYMENT_SUBMITTED,
        OrderState.PAYMENT_VERIFIED,
        OrderState.APPROVED,
        OrderState.COMPLETED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    ],
)
def test_submit_utr_wrong_state_is_rejected_unchanged(uow, service, state):
    customer = make_customer(uow)
    seed_settings(uow)
    order = make_order(uow, customer.user_id, state=state)

    result = service.submit_utr(
        order.order_id, VALID_UTR, Actor.customer(customer.user_id)
    )

    assert isinstance(result, Rejected)
    assert result.code == PAYMENT_NOT_PENDING
    # State unchanged and no UTR recorded.
    assert uow.orders.get(order.order_id).state is state
    assert uow.payments.get_by_order(order.order_id) is None


# --------------------------------------------------------------------------- #
# Invalid format against configured + customized patterns (Req 6.2, 6.6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_utr",
    [
        "",  # empty
        "SHORT",  # too few chars
        "ABC123def4567",  # 13 chars (too long)
        "ABC123def45!",  # 12 chars but non-alphanumeric
        "ABC 23def456",  # contains a space
    ],
)
def test_submit_utr_invalid_format_default_pattern(uow, service, bad_utr):
    customer = make_customer(uow)
    seed_settings(uow)  # default '^[A-Za-z0-9]{12}$'
    order = make_order(uow, customer.user_id)

    result = service.submit_utr(
        order.order_id, bad_utr, Actor.customer(customer.user_id)
    )

    assert isinstance(result, Rejected)
    assert result.code == INVALID_UTR_FORMAT
    assert result.details["pattern"] == DEFAULT_UTR_PATTERN
    # State unchanged; nothing recorded.
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING
    assert uow.payments.get_by_order(order.order_id) is None


def test_submit_utr_validates_against_customized_pattern(uow, service):
    customer = make_customer(uow)
    # Operator customizes the pattern to UTR-prefixed exactly-10-digit refs.
    custom = r"^UTR\d{10}$"
    seed_settings(uow, utr_pattern=custom)
    order = make_order(uow, customer.user_id)

    # A value valid under the DEFAULT pattern is now rejected.
    rejected = service.submit_utr(
        order.order_id, VALID_UTR, Actor.customer(customer.user_id)
    )
    assert isinstance(rejected, Rejected)
    assert rejected.code == INVALID_UTR_FORMAT
    assert rejected.details["pattern"] == custom

    # A value matching the customized pattern is accepted.
    accepted = service.submit_utr(
        order.order_id, "UTR0123456789", Actor.customer(customer.user_id)
    )
    assert isinstance(accepted, Order)
    assert accepted.state is OrderState.PAYMENT_SUBMITTED


# --------------------------------------------------------------------------- #
# Duplicate UTR conflict (Req 6.4)
# --------------------------------------------------------------------------- #
def test_submit_utr_duplicate_against_different_order_conflicts(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    seed_settings(uow)
    first = make_order(uow, customer.user_id)
    second = make_order(uow, customer.user_id)

    ok = service.submit_utr(first.order_id, VALID_UTR, Actor.customer(customer.user_id))
    assert isinstance(ok, Order)

    dup = service.submit_utr(
        second.order_id, VALID_UTR, Actor.customer(customer.user_id)
    )
    assert isinstance(dup, Conflict)
    assert dup.code == DUPLICATE_UTR
    # Second order left untouched.
    assert uow.orders.get(second.order_id).state is OrderState.PAYMENT_PENDING
    assert uow.payments.get_by_order(second.order_id) is None


# --------------------------------------------------------------------------- #
# attach_screenshot accept / reject (Req 6.3, 6.8)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/webp"])
def test_attach_screenshot_accepts_supported_formats(uow, service, store, content_type):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)
    data = b"\x89PNG-binary-blob"

    result = service.attach_screenshot(
        order.order_id, data, content_type, size=len(data)
    )

    assert isinstance(result, Payment)
    assert result.screenshot_object_key is not None
    # Only the key is on the payment row; the blob lives in object storage.
    assert result.screenshot_object_key in store
    assert store.get(result.screenshot_object_key).data == data
    assert store.get(result.screenshot_object_key).content_type == content_type
    # The DB row carries no blob bytes -- only the reference.
    persisted = uow.payments.get_by_order(order.order_id)
    assert persisted.screenshot_object_key == result.screenshot_object_key


def test_attach_screenshot_accepts_size_at_exact_boundary(uow, service, store):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)

    result = service.attach_screenshot(
        order.order_id, b"x", "image/jpeg", size=MAX_SCREENSHOT_BYTES
    )
    assert isinstance(result, Payment)
    assert result.screenshot_object_key is not None


def test_attach_screenshot_rejects_unsupported_format(uow, service, store):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)

    result = service.attach_screenshot(
        order.order_id, b"%PDF-1.7", "application/pdf", size=8
    )
    assert isinstance(result, Rejected)
    assert result.code == UNSUPPORTED_IMAGE_FORMAT
    assert "image/jpeg" in result.details["accepted_formats"]
    assert result.details["max_bytes"] == MAX_SCREENSHOT_BYTES
    # Nothing stored, no payment row mutated.
    assert len(store) == 0
    assert uow.payments.get_by_order(order.order_id) is None


def test_attach_screenshot_rejects_oversized_blob(uow, service, store):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)

    result = service.attach_screenshot(
        order.order_id, b"x", "image/png", size=MAX_SCREENSHOT_BYTES + 1
    )
    assert isinstance(result, Rejected)
    assert result.code == IMAGE_TOO_LARGE
    assert result.details["max_bytes"] == MAX_SCREENSHOT_BYTES
    assert len(store) == 0
    assert uow.payments.get_by_order(order.order_id) is None


def test_attach_screenshot_missing_order_is_not_found(uow, service):
    result = service.attach_screenshot(new_id(), b"x", "image/png", size=1)
    assert isinstance(result, NotFound)


def test_attach_screenshot_requires_object_store(uow):
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id)
    svc = PaymentService(uow)  # no object store
    with pytest.raises(ValueError):
        svc.attach_screenshot(order.order_id, b"x", "image/png", size=1)
