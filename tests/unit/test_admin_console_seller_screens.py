"""Unit tests for Admin_Console Task 20.1: Seller-only entry points.

Covers:
  * ``list_pending_verifications`` -- returns PAYMENT_SUBMITTED orders with
    correct UTR/screenshot_key/total and fulfillability flags (Req 7.1, 16.3);
    non-Seller is rejected with NotAuthorized (Req 1.7, 12.1).
  * ``list_active_orders`` -- returns non-terminal orders only, enriched with
    fulfillability; terminal-state orders are excluded (Req 10.6, 16.3, 16.4);
    non-Seller is rejected (Req 12.1).
  * ``set_offline_payment_allowed`` -- delegates to AuthService and returns the
    updated User; non-Seller is rejected (Req 17.2); unknown customer yields
    NotFound (Req 17.3).
  * Fulfillability flag set correctly for an order with a stock shortfall (Req
    16.3/16.4); fulfillable flag is True when stock is sufficient (Req 16.4).
  * ``mark_ready`` attaches shortfall info to ReadyResult (Req 16.3/16.5).

All tests use the in-memory UnitOfWork and a real AuthService (config-driven
Seller gate) to exercise the full domain logic without any database or I/O.

Requirements: 1.7, 7.1, 10.6, 12.1, 16.3, 16.4, 16.5, 17.2, 17.3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from marketplace.admin import AdminConsole, ReadyResult, SellerOrderView
from marketplace.auth.service import AuthService
from marketplace.domain.entities import (
    Category,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import NotAuthorized, NotFound
from marketplace.fulfillability.engine import Shortfall

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SELLER_TID = 10
CUSTOMER_TID = 20
CUSTOMER2_TID = 30

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 32


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def uow(db: InMemoryDatabase) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(db)


@pytest.fixture
def auth(db: InMemoryDatabase) -> AuthService:
    return AuthService(lambda: InMemoryUnitOfWork(db), seller_telegram_id=SELLER_TID)


@pytest.fixture
def console(uow: InMemoryUnitOfWork, auth: AuthService) -> AdminConsole:
    return AdminConsole(uow, auth)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_seller(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=SELLER_TID,
            role=Role.ADMIN,
            verified_contact="+910000000001",
            contact_verified_at=_now(),
        )
    )


def make_customer(
    uow: InMemoryUnitOfWork, telegram_id: int = CUSTOMER_TID
) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=telegram_id,
            role=Role.CUSTOMER,
            verified_contact=f"+9199{telegram_id:07d}",
            contact_verified_at=_now(),
        )
    )


def make_product(
    uow: InMemoryUnitOfWork, stock: Decimal = Decimal("100")
) -> Product:
    cat = uow.catalog.add_category(Category(category_id=new_id(), name="Feed"))
    return uow.catalog.add_product(
        Product(
            product_id=new_id(),
            name="Cotton Seed Cake",
            category_id=cat.category_id,
            unit=Unit.KILOGRAM,
            price_per_unit=Decimal("50.00"),
            min_order_quantity=Decimal("1"),
            stock_quantity=stock,
        )
    )


def make_order(
    uow: InMemoryUnitOfWork,
    customer: User,
    state: OrderState,
    product: Product,
    ordered_qty: Decimal = Decimal("10"),
    created_at: datetime = None,
) -> Order:
    """Insert an order directly into the in-memory store, bypassing the state machine."""
    unit_price = product.price_per_unit
    line_amount = (ordered_qty * unit_price).quantize(Decimal("0.01"))
    return uow.orders.add(
        Order(
            order_id=new_id(),
            customer_id=customer.user_id,
            state=state,
            total_amount=line_amount,
            items=[
                OrderItem(
                    product_id=product.product_id,
                    ordered_quantity=ordered_qty,
                    unit_price=unit_price,
                    line_amount=line_amount,
                )
            ],
            created_at=created_at or _now(),
        )
    )


def make_payment(
    uow: InMemoryUnitOfWork,
    order: Order,
    utr: str = "UTR123456789A",
    screenshot_key: str = None,
) -> Payment:
    return uow.payments.add(
        Payment(
            payment_id=new_id(),
            order_id=order.order_id,
            utr=utr,
            utr_submitted_at=_now(),
            screenshot_object_key=screenshot_key,
        )
    )


# ===========================================================================
# list_pending_verifications
# ===========================================================================
class TestListPendingVerifications:
    def test_returns_payment_submitted_orders_only(self, console, uow):
        """Only PAYMENT_SUBMITTED orders appear in the pending-verifications list (Req 7.1)."""
        seller = make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        # PAYMENT_SUBMITTED → should appear
        submitted = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product)
        make_payment(uow, submitted, utr="UTR000000000A")

        # PAYMENT_PENDING → should NOT appear
        make_order(uow, customer, OrderState.PAYMENT_PENDING, product)

        result = console.list_pending_verifications(SELLER_TID)

        assert isinstance(result, list)
        assert len(result) == 1
        view = result[0]
        assert isinstance(view, SellerOrderView)
        assert view.order.order_id == submitted.order_id

    def test_includes_correct_payment_details(self, console, uow):
        """Each entry carries the UTR, total amount, and screenshot key from the
        payment record (Req 7.1)."""
        seller = make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product,
                           ordered_qty=Decimal("5"))
        make_payment(uow, order, utr="UTR999888777A", screenshot_key="sc/img.png")

        result = console.list_pending_verifications(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.utr == "UTR999888777A"
        assert view.screenshot_key == "sc/img.png"
        assert view.order.total_amount == Decimal("250.00")  # 5 * 50.00

    def test_utr_and_screenshot_none_when_no_payment_record(self, console, uow):
        """An order without a payment record gets utr=None and screenshot_key=None."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        # Insert a PAYMENT_SUBMITTED order but deliberately add no payment row.
        make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product)

        result = console.list_pending_verifications(SELLER_TID)

        assert len(result) == 1
        assert result[0].utr is None
        assert result[0].screenshot_key is None

    def test_rejects_non_seller_with_not_authorized(self, console, uow):
        """A non-Seller caller is rejected with NotAuthorized (Req 1.7, 12.1)."""
        make_customer(uow)

        result = console.list_pending_verifications(CUSTOMER_TID)

        assert isinstance(result, NotAuthorized)

    def test_empty_list_when_no_payment_submitted_orders(self, console, uow):
        """Returns an empty list when no orders are in PAYMENT_SUBMITTED state."""
        make_seller(uow)

        result = console.list_pending_verifications(SELLER_TID)

        assert result == []

    def test_multiple_orders_all_included(self, console, uow):
        """All PAYMENT_SUBMITTED orders are returned, each as a separate entry."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("200"))

        for i in range(3):
            order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product)
            make_payment(uow, order, utr=f"UTR00000000{i:02d}")

        result = console.list_pending_verifications(SELLER_TID)

        assert len(result) == 3
        assert all(isinstance(v, SellerOrderView) for v in result)


# ===========================================================================
# list_pending_verifications — fulfillability flag
# ===========================================================================
class TestPendingVerificationsFulfillability:
    def test_fulfillable_flag_true_when_stock_sufficient(self, console, uow):
        """fulfillable=True when ordered qty ≤ current stock (Req 16.4)."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product,
                           ordered_qty=Decimal("10"))
        make_payment(uow, order)

        result = console.list_pending_verifications(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.fulfillable is True
        assert view.shortfalls == ()

    def test_fulfillable_flag_false_when_stock_shortfall(self, console, uow):
        """fulfillable=False and shortfalls populated when ordered qty > current
        stock (Req 16.3/16.4); this NEVER blocks approval (Req 16.5)."""
        make_seller(uow)
        customer = make_customer(uow)
        # Product has only 5 in stock; order requests 20.
        product = make_product(uow, stock=Decimal("5"))

        order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product,
                           ordered_qty=Decimal("20"))
        make_payment(uow, order)

        result = console.list_pending_verifications(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.fulfillable is False
        assert len(view.shortfalls) == 1
        sf = view.shortfalls[0]
        assert isinstance(sf, Shortfall)
        assert sf.product_id == product.product_id
        assert sf.ordered_quantity == Decimal("20")
        assert sf.current_stock == Decimal("5")

    def test_flag_does_not_block_approval_logic(self, console, uow):
        """The flag is present but the method still returns a list (never a failure)
        regardless of fulfillability (Req 16.5)."""
        make_seller(uow)
        customer = make_customer(uow)
        # Zero stock — maximally unfulfillable.
        product = make_product(uow, stock=Decimal("0"))

        order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product,
                           ordered_qty=Decimal("50"))
        make_payment(uow, order)

        result = console.list_pending_verifications(SELLER_TID)

        # Still returns a list — the flag is informational, never a blocker.
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].fulfillable is False


# ===========================================================================
# list_active_orders
# ===========================================================================
class TestListActiveOrders:
    def test_excludes_terminal_states(self, console, uow):
        """COMPLETED, CANCELLED, and REJECTED orders are not included (Req 10.6)."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        active = make_order(uow, customer, OrderState.PAYMENT_PENDING, product)
        # Terminal states — must be excluded.
        make_order(uow, customer, OrderState.COMPLETED, product)
        make_order(uow, customer, OrderState.CANCELLED, product)
        make_order(uow, customer, OrderState.REJECTED, product)

        result = console.list_active_orders(SELLER_TID)

        assert isinstance(result, list)
        order_ids = {v.order.order_id for v in result}
        assert active.order_id in order_ids
        # None of the terminal orders should appear.
        assert len(result) == 1

    def test_includes_all_non_terminal_states(self, console, uow):
        """All pre-terminal states appear in the active-orders list."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("1000"))

        non_terminal = [
            OrderState.PLACED,
            OrderState.PAYMENT_PENDING,
            OrderState.PAYMENT_SUBMITTED,
            OrderState.PAYMENT_VERIFIED,
            OrderState.APPROVED,
            OrderState.READY_FOR_PICKUP,
        ]
        orders = [make_order(uow, customer, s, product) for s in non_terminal]

        result = console.list_active_orders(SELLER_TID)

        result_ids = {v.order.order_id for v in result}
        for order in orders:
            assert order.order_id in result_ids

    def test_rejects_non_seller(self, console, uow):
        """A non-Seller is rejected with NotAuthorized (Req 12.1)."""
        make_customer(uow)

        result = console.list_active_orders(CUSTOMER_TID)

        assert isinstance(result, NotAuthorized)

    def test_result_carries_fulfillability_for_each_order(self, console, uow):
        """Each SellerOrderView carries a fulfillable flag (Req 16.3/16.4)."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        make_order(uow, customer, OrderState.PAYMENT_PENDING, product,
                   ordered_qty=Decimal("5"))

        result = console.list_active_orders(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert isinstance(view, SellerOrderView)
        assert view.fulfillable is True  # 5 ≤ 100

    def test_fulfillability_flag_false_for_shortfall_order(self, console, uow):
        """An active order whose qty exceeds stock gets fulfillable=False (Req 16.3)."""
        make_seller(uow)
        customer = make_customer(uow)
        # 3 in stock, order requests 50.
        product = make_product(uow, stock=Decimal("3"))

        make_order(uow, customer, OrderState.PLACED, product,
                   ordered_qty=Decimal("50"))

        result = console.list_active_orders(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.fulfillable is False
        assert len(view.shortfalls) == 1
        sf = view.shortfalls[0]
        assert sf.ordered_quantity == Decimal("50")
        assert sf.current_stock == Decimal("3")

    def test_returns_view_with_payment_details_when_payment_exists(self, console, uow):
        """Payment details are populated when a payment record exists."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        order = make_order(uow, customer, OrderState.PAYMENT_SUBMITTED, product)
        make_payment(uow, order, utr="UTR123456789A", screenshot_key="sc/abc.png")

        result = console.list_active_orders(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.utr == "UTR123456789A"
        assert view.screenshot_key == "sc/abc.png"

    def test_payment_details_none_for_order_without_payment(self, console, uow):
        """utr and screenshot_key are None for a PLACED order (no payment yet)."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))

        make_order(uow, customer, OrderState.PLACED, product)

        result = console.list_active_orders(SELLER_TID)

        assert len(result) == 1
        view = result[0]
        assert view.utr is None
        assert view.screenshot_key is None


# ===========================================================================
# set_offline_payment_allowed
# ===========================================================================
class TestSetOfflinePaymentAllowed:
    def test_seller_can_enable_flag_for_customer(self, console, uow, auth):
        """Seller enables the flag and the updated User is returned (Req 17.1)."""
        make_seller(uow)
        customer = make_customer(uow)
        assert not customer.offline_payment_allowed

        result = console.set_offline_payment_allowed(
            customer.user_id, True, SELLER_TID
        )

        assert isinstance(result, User)
        assert result.offline_payment_allowed is True

    def test_seller_can_disable_flag_for_customer(self, console, uow):
        """Seller disables a previously-enabled flag (Req 17.1)."""
        make_seller(uow)
        # Create customer with flag already enabled.
        customer = uow.users.add(
            User(
                user_id=new_id(),
                telegram_user_id=CUSTOMER_TID,
                role=Role.CUSTOMER,
                verified_contact="+919999999999",
                contact_verified_at=_now(),
                offline_payment_allowed=True,
            )
        )

        result = console.set_offline_payment_allowed(
            customer.user_id, False, SELLER_TID
        )

        assert isinstance(result, User)
        assert result.offline_payment_allowed is False

    def test_rejects_non_seller_and_leaves_flag_unchanged(self, console, uow):
        """A non-Seller call is rejected; the flag is NOT changed (Req 17.2)."""
        make_customer(uow)
        customer2 = make_customer(uow, telegram_id=CUSTOMER2_TID)

        result = console.set_offline_payment_allowed(
            customer2.user_id, True, CUSTOMER_TID
        )

        assert isinstance(result, NotAuthorized)
        # The customer's flag should remain False.
        stored = uow.users.get(customer2.user_id)
        assert stored.offline_payment_allowed is False

    def test_returns_not_found_for_unknown_customer(self, console, uow):
        """An unknown customer reference yields NotFound (Req 17.3)."""
        make_seller(uow)

        result = console.set_offline_payment_allowed(
            new_id(), True, SELLER_TID  # random UUID — no such user
        )

        assert isinstance(result, NotFound)

    def test_accepts_telegram_id_as_customer_ref(self, console, uow):
        """Telegram user id (int) works as the target_customer_ref."""
        make_seller(uow)
        make_customer(uow)

        result = console.set_offline_payment_allowed(
            CUSTOMER_TID, True, SELLER_TID
        )

        assert isinstance(result, User)
        assert result.offline_payment_allowed is True

    def test_accepts_user_object_as_acting_user(self, console, uow):
        """acting_user may be a User object instead of a raw Telegram id."""
        seller = make_seller(uow)
        make_customer(uow)

        result = console.set_offline_payment_allowed(
            CUSTOMER_TID, True, seller
        )

        assert isinstance(result, User)
        assert result.offline_payment_allowed is True


# ===========================================================================
# mark_ready — fulfillability shortfall attachment (optional enhancement)
# ===========================================================================
class TestMarkReadyWithFulfillability:
    def _approved_order(self, uow: InMemoryUnitOfWork, customer: User,
                        product: Product,
                        ordered_qty: Decimal = Decimal("10")) -> Order:
        unit_price = product.price_per_unit
        line_amount = (ordered_qty * unit_price).quantize(Decimal("0.01"))
        return uow.orders.add(
            Order(
                order_id=new_id(),
                customer_id=customer.user_id,
                state=OrderState.APPROVED,
                total_amount=line_amount,
                items=[
                    OrderItem(
                        product_id=product.product_id,
                        ordered_quantity=ordered_qty,
                        unit_price=unit_price,
                        line_amount=line_amount,
                    )
                ],
                created_at=_now(),
            )
        )

    def test_mark_ready_attaches_fulfillable_true_when_stock_sufficient(
        self, console, uow
    ):
        """ReadyResult.fulfillable=True when current stock covers the order."""
        make_seller(uow)
        customer = make_customer(uow)
        product = make_product(uow, stock=Decimal("100"))
        uow.seller_settings.upsert(
            SellerSettings(seller_settings_id=new_id(), pickup_location="Shop 1")
        )
        order = self._approved_order(uow, customer, product, ordered_qty=Decimal("5"))

        result = console.mark_ready(order.order_id, SELLER_TID)

        assert isinstance(result, ReadyResult)
        assert result.fulfillable is True
        assert result.fulfillability_shortfalls == ()

    def test_mark_ready_attaches_fulfillable_false_and_shortfalls_when_stock_low(
        self, console, uow
    ):
        """ReadyResult carries shortfall info when stock dipped below the ordered
        qty after approval (Req 16.3/16.5); transition still happens (never blocked)."""
        make_seller(uow)
        customer = make_customer(uow)
        # Simulate stock was decremented at approval and now is 0.
        product = make_product(uow, stock=Decimal("0"))
        uow.seller_settings.upsert(
            SellerSettings(seller_settings_id=new_id(), pickup_location="Shop 1")
        )
        order = self._approved_order(uow, customer, product, ordered_qty=Decimal("5"))

        result = console.mark_ready(order.order_id, SELLER_TID)

        assert isinstance(result, ReadyResult)
        # Transition succeeded (non-blocking).
        assert result.order.state is OrderState.READY_FOR_PICKUP
        # Shortfall data is attached.
        assert result.fulfillable is False
        assert len(result.fulfillability_shortfalls) == 1
        sf = result.fulfillability_shortfalls[0]
        assert isinstance(sf, Shortfall)
        assert sf.current_stock == Decimal("0")
        assert sf.ordered_quantity == Decimal("5")
