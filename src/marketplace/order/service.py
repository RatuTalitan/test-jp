"""Order_Service: order creation and the guarded lifecycle (tasks 10.1/10.2).

This is the channel- and DB-agnostic Order_Service described in design.md ->
``Order_Service (owns the Order State Machine)``. Like the Cart_Service it reaches
persistence **only** through the repository protocols exposed by a
:class:`~marketplace.domain.repositories.UnitOfWork` and never imports the ORM
or opens a connection itself (design.md -> Architectural Principles). The caller
(the Bot_Interface) owns the transaction boundary: it opens the ``UnitOfWork``
for the inbound update and commits/rolls back; this service performs its work
inside that transaction and never commits on its own.

Every state change is routed through the single guarded
:func:`marketplace.order.state_machine.transition` function (task 10.1) -- this
service never assigns ``order.state`` directly.

Following the "stable status codes, never localized prose" rule, business-rule
refusals are returned as typed results
(:class:`~marketplace.domain.results.Rejected` /
:class:`~marketplace.domain.results.Conflict` /
:class:`~marketplace.domain.results.Unauthenticated`) carrying a **stable code**
plus structured ``details`` that the Bot_Interface maps to a localized
Message_Catalog template.

Public API (extended by later tasks 12.x/14.x/16.x/17.x and the Bot_Interface)::

    OrderService(uow).place_order(customer_id) -> Order | Failure   # task 10.2

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 12.6.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Union

from marketplace.domain import money
from marketplace.domain.entities import (
    AuditAction,
    AuditEntry,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderItem,
    OrderState,
    Role,
    User,
    new_id,
)
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import (
    Conflict,
    Failure,
    NotAuthorized,
    NotFound,
    Rejected,
    Unauthenticated,
)
from marketplace.order.state_machine import (
    Actor,
    ActorRole,
    Guards,
    OrderEvent,
    TERMINAL_STATES,
    transition,
)

__all__ = [
    "OrderService",
    "EMPTY_CART",
    "STOCK_EXCEEDED_AT_PLACEMENT",
    "NO_ORDERS",
    "ORDER_NOT_AVAILABLE",
    "PAYMENT_REFERENCE_NOT_PROVIDED",
    "PLACEMENT_TRANSITION_SEQ",
    "STATE_TRANSITION_SEQ",
    "CANCELLATION_SELLER_TRANSITION_SEQ",
    "MODIFICATION_TRANSITION_SEQ_BASE",
    "MODIFY_REQUIRES_SELLER",
    "ORDER_NOT_MODIFIABLE",
    "QTY_BELOW_MOQ",
    "MODIFICATION_STOCK_EXCEEDED",
    "ORDER_MUST_RETAIN_LINE",
    "LINE_ALREADY_EXISTS",
    "PRE_FULFILLMENT_STATES",
    "PRE_DECREMENT_STATES",
    "ModificationOp",
    "OrderModification",
    "ModificationResult",
    "CustomerOrderDetail",
    "PlacementResult",
    "LifecycleResult",
    "CustomerOrderResult",
]

# --- Stable status codes (language-agnostic; localized by the Bot_Interface) --
#: Placement attempted from an empty cart (Req 5.3).
EMPTY_CART = "EMPTY_CART"
#: One or more line quantities exceed current stock at placement (Req 5.4). The
#: ``details["lines"]`` payload identifies every affected line + available stock.
STOCK_EXCEEDED_AT_PLACEMENT = "STOCK_EXCEEDED_AT_PLACEMENT"

#: Stable code the Bot_Interface localizes as the "no orders" message when a
#: customer's order history is empty (Req 10.2). The query itself returns an
#: empty list; this is the code the presentation layer renders for that case.
NO_ORDERS = "NO_ORDERS"
#: Stable code for the **non-disclosing** rejection returned when a customer
#: requests an order that is not theirs (Req 10.5). It carries no order details
#: so a customer can never probe another customer's order through this path.
ORDER_NOT_AVAILABLE = "ORDER_NOT_AVAILABLE"
#: Stable code the Bot_Interface localizes as the "not yet provided" placeholder
#: shown for an order's payment reference when no UTR has been recorded yet
#: (Req 10.4). The domain stays language-agnostic: it reports the *absence* of a
#: reference (``CustomerOrderDetail.payment_reference is None``) via this code.
PAYMENT_REFERENCE_NOT_PROVIDED = "PAYMENT_REFERENCE_NOT_PROVIDED"

#: The ``transition_seq`` used for the new-order notification's idempotency key.
#: Placement performs exactly one state transition (PLACED -> PAYMENT_PENDING),
#: which is sequence ``1``; combined with ``(order_id, NEW_ORDER)`` it makes the
#: enqueue idempotent so a retried placement never doubles the seller's message.
PLACEMENT_TRANSITION_SEQ = 1

#: Deterministic per-state transition sequence used to build idempotent
#: notification keys ``(order_id, kind, transition_seq)`` (design.md ->
#: Notifications: idempotency). Each lifecycle state maps to a stable integer so
#: re-running an operation (e.g. a retried approval) never enqueues a duplicate
#: logical notification. ``PAYMENT_PENDING`` is ``1`` so it agrees with
#: :data:`PLACEMENT_TRANSITION_SEQ` (the placement notification's key).
STATE_TRANSITION_SEQ: dict = {
    OrderState.PLACED: 0,
    OrderState.PAYMENT_PENDING: 1,
    OrderState.PAYMENT_SUBMITTED: 2,
    OrderState.PAYMENT_VERIFIED: 3,
    OrderState.APPROVED: 4,
    OrderState.READY_FOR_PICKUP: 5,
    OrderState.COMPLETED: 6,
    OrderState.CANCELLED: 7,
    OrderState.REJECTED: 8,
}

_ZERO = Decimal("0")

#: The seller cancellation notice (Req 9.6) rides the CANCELLED transition
#: *alongside* the customer's cancellation confirmation (Req 9.3). Both are
#: ``STATE_CHANGE`` rows on the same ``order_id`` at the same (CANCELLED) state,
#: so they would otherwise collide on the ``(order_id, kind, transition_seq)``
#: idempotency key. The seller notice therefore uses a distinct, deterministic
#: sequence (the CANCELLED sequence in a separate high band) so each recipient
#: gets exactly one idempotent notification and a retried cancel never doubles
#: either message.
CANCELLATION_SELLER_TRANSITION_SEQ = 100 + STATE_TRANSITION_SEQ[OrderState.CANCELLED]

# --- Seller order modification (Req 15) ------------------------------------- #
#: The states in which a Seller may modify an order (Req 15.1). "Pre-fulfillment"
#: spans every state up to and including APPROVED; from READY_FOR_PICKUP onward
#: (and in the terminal states) a modification is refused (Req 15.2).
PRE_FULFILLMENT_STATES: frozenset = frozenset(
    {
        OrderState.PLACED,
        OrderState.PAYMENT_PENDING,
        OrderState.PAYMENT_SUBMITTED,
        OrderState.PAYMENT_VERIFIED,
        OrderState.APPROVED,
    }
)

#: The subset of modifiable states in which this order's quantities have **not**
#: yet been decremented from product stock (Req 15.6): adds/increases are only
#: *validated* against current stock here and never decrement it. APPROVED is
#: handled separately because its quantities are already decremented (Req 15.7).
PRE_DECREMENT_STATES: frozenset = frozenset(
    {
        OrderState.PLACED,
        OrderState.PAYMENT_PENDING,
        OrderState.PAYMENT_SUBMITTED,
        OrderState.PAYMENT_VERIFIED,
    }
)

#: A non-Seller attempted a modification (Req 15.3); no data is changed.
MODIFY_REQUIRES_SELLER = "MODIFY_REQUIRES_SELLER"
#: A modification was attempted while the order is in a non-modifiable state
#: (READY_FOR_PICKUP / COMPLETED / CANCELLED / REJECTED) (Req 15.2).
ORDER_NOT_MODIFIABLE = "ORDER_NOT_MODIFIABLE"
#: The resulting line quantity is <= 0 or below the product's
#: Minimum_Order_Quantity (Req 15.5). ``details["min_order_quantity"]`` carries
#: the MOQ for the localized message.
QTY_BELOW_MOQ = "QTY_BELOW_MOQ"
#: An add/increase would make a line's quantity exceed the affected product's
#: current stock (Req 15.6 pre-decrement / Req 15.8 approved). ``details``
#: identifies the product and its available stock; nothing is changed.
MODIFICATION_STOCK_EXCEEDED = "MODIFICATION_STOCK_EXCEEDED"
#: A modification would reduce the order to zero line items (Req 15.10); the
#: Seller is told to cancel or reject the order instead.
ORDER_MUST_RETAIN_LINE = "ORDER_MUST_RETAIN_LINE"
#: An ADD_LINE named a product that is already a line on the order (Req 15.1):
#: the Seller should change that line's quantity instead.
LINE_ALREADY_EXISTS = "LINE_ALREADY_EXISTS"

#: Idempotency-key band for the customer "order modified" notification
#: (Req 15.13). A modification does not change state, and several modifications
#: may occur in the same state, so the per-modification sequence is this base
#: plus the count of modifications already recorded for the order. Using a high
#: band keeps it clear of the STATE_CHANGE (0-8) and cancellation (100+) seqs.
MODIFICATION_TRANSITION_SEQ_BASE = 200


class ModificationOp(str, enum.Enum):
    """The kind of single edit a Seller modification applies (Req 15.1).

    Exactly one operation is applied per :meth:`OrderService.modify` call so that
    a modification maps to exactly one Audit_Trail entry (Req 15.12).
    """

    #: Add a brand-new line for a product not already on the order (Req 15.15).
    ADD_LINE = "ADD_LINE"
    #: Remove an existing line entirely (Req 15.9/15.10).
    REMOVE_LINE = "REMOVE_LINE"
    #: Change an existing line's ordered quantity (Req 15.5/15.7/15.16).
    CHANGE_QTY = "CHANGE_QTY"


@dataclass(frozen=True)
class OrderModification:
    """A single, channel-agnostic order-modification request (Req 15.1).

    Describes one add / remove / change-quantity edit so the Bot_Interface and
    Admin_Console can drive :meth:`OrderService.modify` uniformly:

    * ``ADD_LINE`` -- ``product_id`` of the product to add + ``quantity`` (the
      new line snapshots the product's current catalog price, Req 15.15).
    * ``CHANGE_QTY`` -- ``product_id`` of an existing line + the new
      ``quantity``.
    * ``REMOVE_LINE`` -- ``product_id`` of the line to remove (``quantity``
      ignored).

    ``quantity`` accepts any :class:`~marketplace.domain.money.Numeric` and is
    normalized to :class:`~decimal.Decimal` by the service.
    """

    operation: ModificationOp
    product_id: uuid.UUID
    quantity: Optional[money.Numeric] = None


#: ``modify`` returns the (mutated, persisted) order on success or a typed
#: failure that leaves the order **and** all product stock unchanged.
ModificationResult = Union[Order, Failure]

#: Maps a modification operation to its Audit_Trail action value (Req 15.12).
_MODIFICATION_AUDIT_ACTION: dict = {
    ModificationOp.ADD_LINE: AuditAction.ADD_LINE,
    ModificationOp.REMOVE_LINE: AuditAction.REMOVE_LINE,
    ModificationOp.CHANGE_QTY: AuditAction.MODIFY_LINE,
}

#: A placement returns the created order or one of these typed failures.
PlacementResult = Union[Order, Rejected, Conflict, Unauthenticated, NotFound]

#: A lifecycle operation (verify/approve/reject/mark_ready/mark_collected/cancel)
#: returns the (mutated, persisted) order on success or a typed failure that
#: leaves the order's state unchanged.
LifecycleResult = Union[Order, Failure]


@dataclass(frozen=True)
class CustomerOrderDetail:
    """A customer-facing detail view of one of the customer's own orders (Req 10.3/10.4).

    Bundles the :class:`~marketplace.domain.entities.Order` (line items, total
    amount, current state) with the recorded payment reference read from the
    Payment aggregate. ``payment_reference`` is the recorded UTR, or ``None``
    when none has been submitted yet; in the ``None`` case the Bot_Interface
    renders the localized :data:`PAYMENT_REFERENCE_NOT_PROVIDED` placeholder
    ("not yet provided", Req 10.4) -- the domain never emits user-facing prose.
    """

    order: Order
    payment_reference: Optional[str] = None

    @property
    def has_payment_reference(self) -> bool:
        """True when a UTR has been recorded against the order (Req 10.4)."""
        return self.payment_reference is not None


#: ``get_order_for_customer`` returns the detail view on success, a
#: :class:`~marketplace.domain.results.NotFound` when the order id is unknown, or
#: a non-disclosing :class:`~marketplace.domain.results.NotAuthorized`
#: (``ORDER_NOT_AVAILABLE``) when the order belongs to another customer (Req 10.5).
CustomerOrderResult = Union[CustomerOrderDetail, NotFound, NotAuthorized]


class OrderService:
    """Order operations bound to a single :class:`UnitOfWork` (one per request).

    Constructed with the active ``UnitOfWork``; uses its ``users``, ``carts``,
    ``catalog``, ``orders`` and ``notifications`` repositories. Mutating
    operations leave the transaction open for the caller to commit; a rejected
    operation makes no persistent change.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # ----------------------------------------------------------- place_order
    def place_order(self, customer_id) -> PlacementResult:
        """Create an order from the customer's cart (Req 5.1-5.7, 12.6).

        Validation order (each leaves all data unchanged on failure):

        1. **Authentication (Req 12.6):** the customer must have a
           Verified_Contact; an unauthenticated user is rejected with
           :class:`~marketplace.domain.results.Unauthenticated` and no order is
           created (the Bot_Interface re-presents Share Contact).
        2. **Non-empty cart (Req 5.3):** an empty cart is rejected and left
           unchanged.
        3. **Stock (Req 5.4):** if any line quantity exceeds the product's
           current stock, placement is rejected and the response identifies
           every affected line and that product's available stock.

        On success (Req 5.1/5.2/5.5/5.6):

        * the cart lines are copied into a new :class:`Order` with each line's
          ``unit_price`` snapshotted from the product's current catalog price;
        * each line amount is ``round(qty x unit_price, 2)`` half-up and the
          order total is the sum of the rounded line amounts (Monetary_Rounding,
          task 4.4);
        * a unique order id, the customer id, and a creation timestamp are
          recorded; the order starts PLACED and is **auto-transitioned to
          PAYMENT_PENDING** via the guarded state machine (task 10.1);
        * the customer's cart is cleared (Req 5.6); and
        * a new-order notification is enqueued for the Seller (Req 5.7) with an
          idempotent ``(order_id, NEW_ORDER, transition_seq)`` key.

        Note: presenting the UPI instructions to the customer (Req 5.5) is the
        Payment_Service's concern (task 11.1, triggered by the PAYMENT_PENDING
        state); this method only drives the state and the seller notification.
        """
        # 1. Authentication gate (Req 12.6).
        user = self._uow.users.get(customer_id)
        if user is None or not user.is_authenticated:
            return Unauthenticated(
                reason="a Verified_Contact is required to place an order"
            )

        # 2. Non-empty cart (Req 5.3).
        cart = self._uow.carts.get_by_customer(customer_id)
        if cart is None or not cart.items:
            return Rejected(
                EMPTY_CART,
                reason="cannot place an order from an empty cart",
                details={"customer_id": customer_id},
            )

        # 3. Build snapshot lines while checking stock (Req 5.2/5.4).
        order_items: list[OrderItem] = []
        affected_lines: list[dict] = []
        for item in cart.items:
            product = self._uow.catalog.get_product(item.product_id)
            if product is None:
                # Defensive: a cart line referencing a removed product cannot be
                # priced or stock-checked; surface it rather than silently drop.
                return NotFound("product", item.product_id)

            if item.quantity > product.stock_quantity:
                affected_lines.append(
                    {
                        "product_id": product.product_id,
                        "name": product.name,
                        "ordered_quantity": item.quantity,
                        "available_stock": product.stock_quantity,
                    }
                )
                # Keep scanning so the response lists *every* affected line.
                continue

            unit_price = product.price_per_unit
            order_items.append(
                OrderItem(
                    product_id=product.product_id,
                    ordered_quantity=item.quantity,
                    unit_price=unit_price,
                    line_amount=money.line_amount(item.quantity, unit_price),
                )
            )

        if affected_lines:
            # Leave the cart unchanged and identify every affected line (Req 5.4).
            return Conflict(
                STOCK_EXCEEDED_AT_PLACEMENT,
                reason="one or more line quantities exceed current stock",
                details={"lines": affected_lines},
            )

        # 4. Compose the order: rounded total = sum of rounded line amounts.
        total = money.order_total(line.line_amount for line in order_items)
        order = Order(
            order_id=new_id(),
            customer_id=customer_id,
            state=OrderState.PLACED,
            total_amount=total,
            items=order_items,
            created_at=datetime.now(timezone.utc),
        )

        # 5. Auto-transition PLACED -> PAYMENT_PENDING through the guarded
        # state machine (task 10.1) -- never assign order.state directly.
        moved = transition(
            order, OrderEvent.AUTO_PAYMENT_PENDING, Actor.system(), Guards()
        )
        # The auto edge is unconditional, so this should always succeed; treat a
        # failure defensively as a hard error rather than persisting a half-made
        # order.
        if not isinstance(moved, Order):
            return moved
        order = moved

        # 6. Persist the order, clear the cart (Req 5.6), enqueue the seller
        # notification (Req 5.7). All within the caller's open transaction.
        stored = self._uow.orders.add(order)
        self._uow.carts.delete(cart.cart_id)
        self._enqueue_new_order_notification(stored)
        return stored

    # --------------------------------------------- verify -> approve cascade
    def verify_payment(self, order_id, actor: Actor) -> LifecycleResult:
        """Seller verifies a submitted payment, cascading into approval (Req 7.2/7.3/7.4).

        Standard (non-offline) path: the Seller marks a PAYMENT_SUBMITTED order's
        payment as verified. This drives PAYMENT_SUBMITTED -> PAYMENT_VERIFIED
        through the guarded state machine (Req 7.2) and then immediately attempts
        the approval cascade (Req 7.3): "WHEN an Order transitions to Payment
        Verified, THE Order_Service SHALL transition to Approved and reduce
        stock" -- provided every line fits current stock.

        Returns:
            * the **APPROVED** order when verification and the atomic stock
              decrement both succeed;
            * a :class:`~marketplace.domain.results.Conflict` (code
              ``STOCK_CONFLICT``) when verification succeeds but a line exceeds
              current stock -- the order is left in **PAYMENT_VERIFIED**, no
              stock changes, and the Seller is notified of the affected lines
              (Req 7.4);
            * a state-preserving failure (wrong state / wrong actor) when the
              order is not awaiting verification (Req 7.7).

        The order is reached only through :func:`transition`; this method never
        assigns ``order.state`` directly. The whole operation runs inside the
        caller's open Unit-of-Work transaction.
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        verified = transition(order, OrderEvent.VERIFY, actor, Guards())
        if not isinstance(verified, Order):
            # Wrong state (not PAYMENT_SUBMITTED, Req 7.7) or wrong actor: the
            # state is left unchanged by the state machine.
            return verified

        # Persist the PAYMENT_VERIFIED transition, then cascade to approval.
        self._uow.orders.update(verified)
        return self.approve(order_id, actor)

    def approve(self, order_id, actor: Actor) -> LifecycleResult:
        """Atomically decrement stock and approve a PAYMENT_VERIFIED order (Req 7.3/7.4/16.5).

        This is the single place stock is ever decremented (design.md ->
        Order_Service: "Stock decrement happens **only** on Payment Verified ->
        Approved"). In one transaction it:

        1. Locks the involved product rows via
           :meth:`CatalogRepository.lock_products_for_update`
           (``SELECT ... FOR UPDATE`` ordered by ``product_id`` on the
           SQLAlchemy backend; an atomic re-read in memory) so two concurrent
           approvals serialize and can never both observe pre-decrement stock
           (the global no-oversell invariant, Req 16.5).
        2. Re-reads each line's product's **current** stock.
        3. If **every** line's ordered quantity is ``<=`` current stock:
           transitions PAYMENT_VERIFIED -> APPROVED (via :func:`transition` with
           ``stock_ok=True``), decrements each product by the ordered quantity
           (never below zero -- the DB ``CHECK (stock >= 0)`` is defence in
           depth), persists, and notifies the Customer the order is approved
           (Req 7.8).
        4. If **any** line exceeds current stock: leaves the order in
           PAYMENT_VERIFIED, changes **no** stock, notifies the Seller listing
           each affected line and its available stock, and returns a
           :class:`~marketplace.domain.results.Conflict` (Req 7.4).

        Re-runnable: after a stock conflict the Seller can restock and call
        :meth:`approve` again (the APPROVE edge is legal from PAYMENT_VERIFIED).
        Returns a state-preserving failure if the order is not in
        PAYMENT_VERIFIED or the actor is not the Seller.
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        # 1. Lock the involved products (ordered by id) and re-read their stock.
        product_ids = {item.product_id for item in order.items}
        locked = self._uow.catalog.lock_products_for_update(product_ids)
        locked_by_id = {p.product_id: p for p in locked}

        # 2. Re-check every line against the freshly-locked stock.
        affected_lines: list[dict] = []
        for item in order.items:
            product = locked_by_id.get(item.product_id)
            if product is None:
                # A line referencing a removed product can't be approved.
                return NotFound("product", item.product_id)
            if item.ordered_quantity > product.stock_quantity:
                affected_lines.append(
                    {
                        "product_id": product.product_id,
                        "name": product.name,
                        "ordered_quantity": item.ordered_quantity,
                        "available_stock": product.stock_quantity,
                    }
                )

        stock_ok = not affected_lines

        # 3/4. Route the decision through the guarded state machine. With
        # ``stock_ok=False`` the APPROVE guard returns the STOCK_CONFLICT and the
        # state is preserved; with ``stock_ok=True`` the edge advances to
        # APPROVED. A wrong state/actor yields its own state-preserving failure.
        result = transition(
            order,
            OrderEvent.APPROVE,
            actor,
            Guards(
                stock_ok=stock_ok,
                stock_details={
                    "order_id": order.order_id,
                    "lines": affected_lines,
                },
            ),
        )

        if isinstance(result, Conflict):
            # Stock conflict (Req 7.4): order stays PAYMENT_VERIFIED, no stock
            # change; notify the Seller of the affected lines + available stock.
            self._enqueue_seller_stock_conflict(order, affected_lines)
            return result
        if not isinstance(result, Order):
            # Wrong state (not PAYMENT_VERIFIED) or wrong actor (Req 7.7).
            return result

        approved = result
        # Decrement each product by the ordered quantity (defence-in-depth: the
        # re-check above already guarantees this never drives stock negative).
        for item in approved.items:
            product = locked_by_id[item.product_id]
            product.stock_quantity = product.stock_quantity - item.ordered_quantity
            if product.stock_quantity < _ZERO:  # pragma: no cover - guarded above
                product.stock_quantity = _ZERO
            self._uow.catalog.update_product(product)

        stored = self._uow.orders.update(approved)
        self._enqueue_customer_state_change(stored)  # "approved" (Req 7.8)
        return stored

    # ------------------------------------------ rejection + fulfillment edges
    def reject_payment(self, order_id, actor: Actor, reason: str) -> LifecycleResult:
        """Seller rejects a submitted payment with a reason (Req 7.5/7.6/7.9).

        From PAYMENT_SUBMITTED, requires a 1-500 character ``reason`` (validated
        by the REJECT guard). On success the order transitions to REJECTED, the
        reason is recorded on the order, and the Customer is notified of the
        rejection and the recorded reason (Req 7.9). A missing/oversized reason
        or a wrong starting state leaves the order unchanged (Req 7.6/7.7).
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        result = transition(
            order, OrderEvent.REJECT, actor, Guards(rejection_reason=reason)
        )
        if not isinstance(result, Order):
            return result

        stored = self._uow.orders.update(result)
        self._enqueue_customer_state_change(
            stored, extra={"rejection_reason": stored.rejection_reason}
        )
        return stored

    def mark_ready(self, order_id, actor: Actor) -> LifecycleResult:
        """Seller marks an APPROVED order ready for pickup (Req 8.1/8.2).

        Transitions APPROVED -> READY_FOR_PICKUP and notifies the Customer of the
        Seller-configured Pickup_Location (read from ``seller_settings``; the
        non-blocking "no pickup location set" warning is task 20.2's concern, so
        here the location is simply included when set) together with each line
        item's product and ordered quantity (Req 8.2). A wrong starting state
        leaves the order unchanged (Req 8.5).
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        result = transition(order, OrderEvent.MARK_READY, actor, Guards())
        if not isinstance(result, Order):
            return result

        stored = self._uow.orders.update(result)
        settings = self._uow.seller_settings.get()
        pickup_location = settings.pickup_location if settings is not None else None
        self._enqueue_customer_state_change(
            stored,
            extra={
                "pickup_location": pickup_location,
                "items": [
                    {
                        "product_id": str(item.product_id),
                        "ordered_quantity": str(item.ordered_quantity),
                    }
                    for item in stored.items
                ],
            },
        )
        return stored

    def mark_collected(self, order_id, actor: Actor) -> LifecycleResult:
        """Seller marks a READY_FOR_PICKUP order collected/complete (Req 8.3/8.6).

        Transitions READY_FOR_PICKUP -> COMPLETED and notifies the Customer the
        order is complete (Req 8.6). A wrong starting state leaves the order
        unchanged (Req 8.4).
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        result = transition(order, OrderEvent.MARK_COLLECTED, actor, Guards())
        if not isinstance(result, Order):
            return result

        stored = self._uow.orders.update(result)
        self._enqueue_customer_state_change(stored)
        return stored

    # --------------------------------------------------- customer cancellation
    def cancel(self, order_id, actor: Actor) -> LifecycleResult:
        """Customer cancels a pre-approval order (Req 9.1-9.6).

        Cancellation is permitted **only** when the actor is the customer who
        placed the order *and* the order is still in PLACED, PAYMENT_PENDING, or
        PAYMENT_SUBMITTED -- both conditions are enforced by the single guarded
        CANCEL edge of the state machine (the edge restricts the legal
        ``from`` states and its ownership guard checks ``actor.user_id ==
        order.customer_id``). This service never assigns ``order.state``
        directly.

        Outcomes (each failure leaves the order's state unchanged):

        * **Success (Req 9.1/9.2):** the order transitions to CANCELLED, the
          cancelling Customer is sent a confirmation notification (Req 9.3) and
          the Seller is notified of the cancellation (Req 9.6).
        * **Non-owner (Req 9.5):** a customer who did not place the order gets a
          :class:`~marketplace.domain.results.NotAuthorized` (``NOT_ORDER_OWNER``)
          from the ownership guard and nothing changes.
        * **Non-cancellable state (Req 9.4):** an order already Approved, Ready
          For Pickup, Completed, or Cancelled has no CANCEL edge, so a
          state-preserving :class:`~marketplace.domain.results.Rejected`
          (``INVALID_TRANSITION``) is returned and nothing changes.

        Both notifications are enqueued via ``uow.notifications`` with idempotent
        ``(order_id, kind, transition_seq)`` keys, so a retried cancel never
        doubles either message.
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        result = transition(order, OrderEvent.CANCEL, actor, Guards())
        if not isinstance(result, Order):
            # Non-owner -> NotAuthorized(NOT_ORDER_OWNER) (Req 9.5); a
            # non-cancellable state -> Rejected(INVALID_TRANSITION) (Req 9.4).
            # Either way the state machine left the order untouched.
            return result

        stored = self._uow.orders.update(result)
        self._enqueue_customer_state_change(stored)  # confirmation (Req 9.3)
        self._enqueue_seller_cancellation(stored)  # seller notice (Req 9.6)
        return stored

    # --------------------------------------------------- seller modification
    def modify(
        self, order_id, modification: OrderModification, actor: Actor
    ) -> ModificationResult:
        """Seller modifies a pre-fulfillment order's contents (Req 15.1-15.16).

        Applies exactly one ``add line`` / ``remove line`` / ``change quantity``
        operation (the :class:`OrderModification`) and, on success, recomputes
        amounts, records one Audit_Trail entry, and notifies the Customer -- all
        while leaving the order's state unchanged. Every failure leaves the order
        **and** all product stock unchanged.

        Guards (each leaves all data unchanged):

        * **Seller only (Req 15.3):** a non-Seller actor gets
          :class:`~marketplace.domain.results.NotAuthorized`
          (``MODIFY_REQUIRES_SELLER``).
        * **Unknown order (Req 15.4):**
          :class:`~marketplace.domain.results.NotFound`.
        * **Modifiable state (Req 15.1/15.2):** only the
          :data:`PRE_FULFILLMENT_STATES` are modifiable; otherwise
          :class:`~marketplace.domain.results.Rejected` (``ORDER_NOT_MODIFIABLE``).
        * **Quantity (Req 15.5):** a resulting line quantity must be ``> 0`` and
          ``>=`` the product's Minimum_Order_Quantity, else ``QTY_BELOW_MOQ``
          (carrying the MOQ).
        * **Minimum one line (Req 15.10):** a removal that would empty the order
          is rejected with ``ORDER_MUST_RETAIN_LINE``.

        Stock handling depends on the state:

        * **Pre-decrement** (PLACED / PAYMENT_PENDING / PAYMENT_SUBMITTED /
          PAYMENT_VERIFIED): an add/increase is *validated* against current stock
          but **never** decrements it (Req 15.6); insufficient stock yields a
          state-preserving :class:`~marketplace.domain.results.Conflict`.
        * **APPROVED** (already decremented): the affected product's stock is
          adjusted by exactly ``(old_qty - new_qty)`` under
          :meth:`CatalogRepository.lock_products_for_update` -- returning stock on
          a reduce/remove (Req 15.9) and deducting on an increase/add (Req 15.7);
          an increase/add beyond available stock is rejected leaving everything
          unchanged (Req 15.8).

        Edit-time pricing (Req 15.15/15.16): a newly added line snapshots the
        product's **current** catalog price; a line whose quantity is changed is
        repriced to the current catalog price; every other (untouched) line keeps
        its original snapshot ``unit_price``. Each ``line_amount`` is recomputed
        as ``round(qty x snapshot_unit_price, 2)`` and the order total as the sum
        of the rounded line amounts via the shared money utility (Req 15.11).
        """
        # 1. Seller-only (Req 15.3).
        if actor.role is not ActorRole.SELLER:
            return NotAuthorized(
                reason="only the Seller may modify an order",
                code=MODIFY_REQUIRES_SELLER,
            )

        # 2. The order must exist (Req 15.4).
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        # 3. The order must be in a modifiable (pre-fulfillment) state (Req 15.2).
        if order.state not in PRE_FULFILLMENT_STATES:
            return Rejected(
                ORDER_NOT_MODIFIABLE,
                reason="the order can no longer be modified in its current state",
                details={"order_id": order.order_id, "state": order.state.value},
            )

        approved = order.state is OrderState.APPROVED
        op = modification.operation
        items = list(order.items)
        existing = next(
            (it for it in items if it.product_id == modification.product_id), None
        )

        # The signed change to apply to the affected product's stock in the
        # APPROVED state: positive returns stock, negative deducts (Req 15.7/15.9).
        stock_delta = _ZERO
        affected_product: Optional = None
        audit_old: Optional[Decimal] = None
        audit_new: Optional[Decimal] = None

        if op is ModificationOp.REMOVE_LINE:
            if existing is None:
                return NotFound("order_line", modification.product_id)
            new_items = [
                it for it in items if it.product_id != modification.product_id
            ]
            # A modification may never empty the order (Req 15.10).
            if not new_items:
                return Rejected(
                    ORDER_MUST_RETAIN_LINE,
                    reason=(
                        "an order must retain at least one line item; "
                        "cancel or reject the order instead"
                    ),
                    details={"order_id": order.order_id},
                )
            audit_old, audit_new = existing.ordered_quantity, None
            if approved:
                # Removing an approved line returns its quantity to stock (Req 15.9).
                product = self._lock_one(modification.product_id)
                if product is None:
                    return NotFound("product", modification.product_id)
                affected_product = product
                stock_delta = existing.ordered_quantity

        elif op in (ModificationOp.ADD_LINE, ModificationOp.CHANGE_QTY):
            if modification.quantity is None:
                return Rejected(
                    QTY_BELOW_MOQ,
                    reason="a positive resulting quantity is required",
                    details={"product_id": modification.product_id},
                )
            new_qty = money.to_decimal(modification.quantity)

            product = self._lock_one(modification.product_id)
            if product is None:
                return NotFound("product", modification.product_id)
            affected_product = product

            # Resulting quantity must be > 0 and at least the MOQ (Req 15.5).
            if new_qty <= _ZERO or new_qty < product.min_order_quantity:
                return Rejected(
                    QTY_BELOW_MOQ,
                    reason=(
                        "resulting quantity must be greater than zero and at "
                        "least the product's minimum order quantity"
                    ),
                    details={
                        "product_id": product.product_id,
                        "min_order_quantity": str(product.min_order_quantity),
                        "requested_quantity": str(new_qty),
                    },
                )

            if op is ModificationOp.ADD_LINE:
                if existing is not None:
                    return Rejected(
                        LINE_ALREADY_EXISTS,
                        reason=(
                            "the product is already a line on this order; "
                            "change its quantity instead"
                        ),
                        details={"product_id": product.product_id},
                    )
                # The entire new line must fit current stock (Req 15.6/15.8).
                if new_qty > product.stock_quantity:
                    return self._modification_stock_conflict(order, product, new_qty)
                if approved:
                    stock_delta = -new_qty  # deduct the whole new line (Req 15.7)
                # A new line snapshots the product's CURRENT catalog price (Req 15.15).
                unit_price = product.price_per_unit
                new_line = OrderItem(
                    product_id=product.product_id,
                    ordered_quantity=new_qty,
                    unit_price=unit_price,
                    line_amount=money.line_amount(new_qty, unit_price),
                )
                new_items = items + [new_line]
                audit_old, audit_new = None, new_qty
            else:  # CHANGE_QTY
                if existing is None:
                    return NotFound("order_line", modification.product_id)
                old_qty = existing.ordered_quantity
                if approved:
                    increase = new_qty - old_qty
                    # Only the *additional* quantity must fit current stock (Req 15.8).
                    if increase > _ZERO and increase > product.stock_quantity:
                        return self._modification_stock_conflict(
                            order, product, increase
                        )
                    stock_delta = old_qty - new_qty  # +ve returns, -ve deducts
                else:
                    # Pre-decrement: the resulting quantity must fit stock (Req 15.6).
                    if new_qty > product.stock_quantity:
                        return self._modification_stock_conflict(
                            order, product, new_qty
                        )
                # A changed (touched) line is repriced to current catalog price
                # (Req 15.16); untouched lines keep their snapshot below.
                unit_price = product.price_per_unit
                changed_line = OrderItem(
                    product_id=product.product_id,
                    ordered_quantity=new_qty,
                    unit_price=unit_price,
                    line_amount=money.line_amount(new_qty, unit_price),
                )
                new_items = [
                    changed_line if it.product_id == product.product_id else it
                    for it in items
                ]
                audit_old, audit_new = old_qty, new_qty
        else:  # pragma: no cover - ModificationOp is exhaustive
            return Rejected(
                ORDER_NOT_MODIFIABLE,
                reason=f"unsupported modification operation {op!r}",
                details={"order_id": order.order_id},
            )

        # All checks passed -> apply. Untouched lines keep their snapshot
        # unit_price; the total is the sum of the rounded line amounts (Req 15.11).
        order.items = new_items
        order.total_amount = money.order_total(it.line_amount for it in new_items)

        # APPROVED state: adjust the affected product's stock by the exact delta
        # (Req 15.7/15.9). Pre-decrement states touch no stock (Req 15.6).
        if approved and affected_product is not None and stock_delta != _ZERO:
            affected_product.stock_quantity = (
                affected_product.stock_quantity + stock_delta
            )
            if affected_product.stock_quantity < _ZERO:  # pragma: no cover
                affected_product.stock_quantity = _ZERO
            self._uow.catalog.update_product(affected_product)

        # State is unchanged by a modification (Req 15.14).
        stored = self._uow.orders.update(order)
        # Exactly one Audit_Trail entry per modification (Req 15.12).
        self._append_modification_audit(
            stored, op, modification.product_id, audit_old, audit_new, actor
        )
        # Notify the customer of the change + recalculated total (Req 15.13).
        self._enqueue_customer_modification(stored, op, modification.product_id)
        return stored

    def _lock_one(self, product_id):
        """Lock + re-read a single product, returning it or ``None`` if missing."""
        locked = self._uow.catalog.lock_products_for_update({product_id})
        return next((p for p in locked if p.product_id == product_id), None)

    def _modification_stock_conflict(self, order, product, requested) -> Conflict:
        """Build the state-preserving stock conflict for a modification (Req 15.6/15.8)."""
        return Conflict(
            MODIFICATION_STOCK_EXCEEDED,
            reason="the requested quantity exceeds the product's current stock",
            details={
                "order_id": order.order_id,
                "product_id": product.product_id,
                "name": product.name,
                "requested_quantity": str(requested),
                "available_stock": str(product.stock_quantity),
            },
        )

    def _append_modification_audit(
        self, order, op: ModificationOp, product_id, old_value, new_value, actor: Actor
    ) -> AuditEntry:
        """Append exactly one Audit_Trail entry for a completed modification (Req 15.12).

        Records the change applied, the affected line/product, the old and new
        values, the acting Seller, and the timestamp. When the actor carries no
        ``user_id`` the configured Seller's id is recorded as the acting Seller.
        """
        acting_user_id = actor.user_id
        if acting_user_id is None:
            seller = self._find_seller()
            acting_user_id = seller.user_id if seller is not None else None
        entry = AuditEntry(
            audit_id=new_id(),
            order_id=order.order_id,
            action=_MODIFICATION_AUDIT_ACTION[op],
            detail={
                "format_version": 1,
                "operation": op.value,
                "product_id": str(product_id),
                "old_value": None if old_value is None else str(old_value),
                "new_value": None if new_value is None else str(new_value),
                "new_total_amount": str(order.total_amount),
            },
            acting_user_id=acting_user_id,
            created_at=datetime.now(timezone.utc),
        )
        return self._uow.audit.add(entry)

    def _enqueue_customer_modification(
        self, order: Order, op: ModificationOp, product_id
    ) -> Optional[Notification]:
        """Enqueue the Customer "order modified" notification (Req 15.13).

        A modification does not change state and several modifications may occur
        in the same state, so the idempotency key uses
        :data:`MODIFICATION_TRANSITION_SEQ_BASE` plus the number of modifications
        already recorded for the order (read after this modification's audit
        entry is appended). Each distinct modification therefore gets its own key
        -- the customer is notified for every change -- while a retried identical
        run finds the existing row and never doubles the message.
        """
        prior_modifications = len(self._uow.audit.list_by_order(order.order_id))
        transition_seq = MODIFICATION_TRANSITION_SEQ_BASE + prior_modifications
        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id, NotificationKind.MODIFIED, transition_seq
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=order.customer_id,
            kind=NotificationKind.MODIFIED,
            transition_seq=transition_seq,
            payload={
                "format_version": 1,
                "kind": NotificationKind.MODIFIED.value,
                "order_id": str(order.order_id),
                "new_state": order.state.value,
                "operation": op.value,
                "product_id": str(product_id),
                "total_amount": str(order.total_amount),
            },
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)

    # ----------------------------------------------- status queries (Req 10)
    def list_customer_orders(self, customer_id) -> list[Order]:
        """Return a customer's order history, newest-first (Req 10.1/10.2).

        Orders are returned ordered by creation timestamp from most recent to
        oldest (the repository's ``list_by_customer`` ordering contract). When
        the customer has no orders the result is an **empty list**, which the
        Bot_Interface renders as the localized :data:`NO_ORDERS` message
        (Req 10.2) -- the query itself is not a failure.
        """
        return self._uow.orders.list_by_customer(customer_id)

    def get_order_for_customer(self, order_id, customer_id) -> CustomerOrderResult:
        """Return one of the customer's own orders in detail (Req 10.3/10.4/10.5).

        Privacy is enforced before any order data is read out:

        * If the order id is unknown, returns
          :class:`~marketplace.domain.results.NotFound`.
        * If the order belongs to a **different** customer, returns a
          **non-disclosing** :class:`~marketplace.domain.results.NotAuthorized`
          (``ORDER_NOT_AVAILABLE``) carrying **no** order details, so a customer
          can never read another customer's order through this path
          (Req 10.5, 12.2).
        * Otherwise returns a :class:`CustomerOrderDetail` bundling the order
          (line items, total amount, current state) with the recorded payment
          reference read via ``uow.payments.get_by_order``. When no UTR has been
          recorded the ``payment_reference`` is ``None`` and the Bot_Interface
          renders the :data:`PAYMENT_REFERENCE_NOT_PROVIDED` "not yet provided"
          placeholder (Req 10.4).
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)
        if order.customer_id != customer_id:
            # Do not reveal whether/what the order is (Req 10.5, 12.2): no
            # details, only the stable non-disclosing code.
            return NotAuthorized(
                reason="this order is not available to the requesting customer",
                code=ORDER_NOT_AVAILABLE,
            )

        payment = self._uow.payments.get_by_order(order_id)
        payment_reference = payment.utr if payment is not None else None
        return CustomerOrderDetail(order=order, payment_reference=payment_reference)

    def list_active_for_seller(self) -> list[Order]:
        """Return all active (non-terminal) orders, oldest-first (Req 10.6).

        "Active" means every order **not** in a terminal state (Completed,
        Cancelled, or Rejected). Results are ordered by creation timestamp from
        oldest to most recent so the Seller works the queue front-to-back.

        Fulfillability flagging of these orders is Task 20.1's concern; this
        method just returns the active orders in the required order.
        """
        active_states = [s for s in OrderState if s not in TERMINAL_STATES]
        orders = self._uow.orders.list_by_states(active_states)
        # Oldest-first (Req 10.6); orders without a timestamp sort last.
        orders.sort(key=lambda o: (o.created_at is None, o.created_at))
        return orders

    def _find_seller(self) -> Optional[User]:
        """Return the configured Seller (the single ADMIN user), or ``None``.

        The Seller is the recipient of the new-order notification (Req 5.7).
        """
        for candidate in self._uow.users.list_all():
            if candidate.role == Role.ADMIN:
                return candidate
        return None

    def _enqueue_new_order_notification(self, order: Order) -> Optional[Notification]:
        """Enqueue the Seller "new order awaits payment" notification (Req 5.7).

        Writes a PENDING ``notifications`` row in the **same transaction** as the
        placement, keyed idempotently by ``(order_id, NEW_ORDER,
        PLACEMENT_TRANSITION_SEQ)`` so a retried placement never enqueues a
        duplicate (design.md -> Notifications: idempotency). The actual delivery
        loop is the retry worker (task 18.2); this is the enqueue hook the
        Notification_Service (task 18.1) will also drive for later transitions.

        If no Seller is configured yet there is no recipient to notify, so the
        enqueue is skipped (the order is still placed successfully).
        """
        seller = self._find_seller()
        if seller is None:
            return None

        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id, NotificationKind.NEW_ORDER, PLACEMENT_TRANSITION_SEQ
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=seller.user_id,
            kind=NotificationKind.NEW_ORDER,
            transition_seq=PLACEMENT_TRANSITION_SEQ,
            payload={
                "format_version": 1,
                "kind": NotificationKind.NEW_ORDER.value,
                "order_id": str(order.order_id),
                "new_state": order.state.value,
                "total_amount": str(order.total_amount),
            },
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)

    def _enqueue_customer_state_change(
        self, order: Order, extra: Optional[dict] = None
    ) -> Optional[Notification]:
        """Enqueue the Customer state-change notification for ``order``.

        Writes a PENDING ``notifications`` row addressed to the ordering Customer
        in the **same transaction** as the state change, keyed idempotently by
        ``(order_id, STATE_CHANGE, STATE_TRANSITION_SEQ[order.state])`` so a
        retried operation (e.g. a re-attempted approval) never doubles the
        message (design.md -> Notifications: idempotency). ``extra`` merges
        state-specific placeholders into the payload (e.g. the rejection reason
        for Req 7.9, or the pickup location + line items for Req 8.2). The
        Notification_Service (task 18.x) drives actual delivery; this is the
        enqueue hook the lifecycle transitions use.
        """
        transition_seq = STATE_TRANSITION_SEQ[order.state]
        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id, NotificationKind.STATE_CHANGE, transition_seq
        )
        if existing is not None:
            return existing

        payload = {
            "format_version": 1,
            "kind": NotificationKind.STATE_CHANGE.value,
            "order_id": str(order.order_id),
            "new_state": order.state.value,
        }
        if extra:
            payload.update(extra)

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=order.customer_id,
            kind=NotificationKind.STATE_CHANGE,
            transition_seq=transition_seq,
            payload=payload,
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)

    def _enqueue_seller_stock_conflict(
        self, order: Order, affected_lines: list[dict]
    ) -> Optional[Notification]:
        """Notify the Seller that approval is blocked by a stock conflict (Req 7.4).

        Enqueued when verification succeeds but a line exceeds current stock: the
        order stays in PAYMENT_VERIFIED and the Seller is told which lines are
        affected and each product's available stock so they can restock and
        re-approve. Keyed idempotently by ``(order_id, STATE_CHANGE,
        STATE_TRANSITION_SEQ[PAYMENT_VERIFIED])`` so repeated blocked approvals
        never spam the Seller with duplicates.
        """
        seller = self._find_seller()
        if seller is None:
            return None

        transition_seq = STATE_TRANSITION_SEQ[order.state]
        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id, NotificationKind.STATE_CHANGE, transition_seq
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=seller.user_id,
            kind=NotificationKind.STATE_CHANGE,
            transition_seq=transition_seq,
            payload={
                "format_version": 1,
                "kind": NotificationKind.STATE_CHANGE.value,
                "order_id": str(order.order_id),
                "new_state": order.state.value,
                "stock_conflict": True,
                "lines": [
                    {
                        "product_id": str(line["product_id"]),
                        "name": line["name"],
                        "ordered_quantity": str(line["ordered_quantity"]),
                        "available_stock": str(line["available_stock"]),
                    }
                    for line in affected_lines
                ],
            },
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)

    def _enqueue_seller_cancellation(self, order: Order) -> Optional[Notification]:
        """Notify the Seller that the Customer cancelled the order (Req 9.6).

        Enqueued alongside the customer's cancellation confirmation when an order
        transitions to CANCELLED. Because both notifications ride the same
        ``(order_id, STATE_CHANGE)`` at the CANCELLED state, this seller notice
        uses the distinct :data:`CANCELLATION_SELLER_TRANSITION_SEQ` so it never
        collides with the customer's confirmation on the idempotency key; a
        retried cancel therefore never doubles the Seller's message. If no Seller
        is configured there is no recipient and the enqueue is skipped (the order
        is still cancelled successfully).
        """
        seller = self._find_seller()
        if seller is None:
            return None

        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id,
            NotificationKind.STATE_CHANGE,
            CANCELLATION_SELLER_TRANSITION_SEQ,
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=seller.user_id,
            kind=NotificationKind.STATE_CHANGE,
            transition_seq=CANCELLATION_SELLER_TRANSITION_SEQ,
            payload={
                "format_version": 1,
                "kind": NotificationKind.STATE_CHANGE.value,
                "order_id": str(order.order_id),
                "new_state": order.state.value,
                "cancelled": True,
            },
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)
