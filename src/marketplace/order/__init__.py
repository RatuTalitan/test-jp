"""Order_Service subsystem (owns the Order State Machine).

Order creation, guarded state transitions, cancellation, Seller modification,
status queries, and atomic stock decrement at Payment Verified -> Approved.
Language-agnostic.

Public API:
  * :func:`~marketplace.order.state_machine.transition` -- the single guarded
    function through which ``orders.state`` ever changes (task 10.1), plus the
    :data:`~marketplace.order.state_machine.ALLOWED_TRANSITIONS` table,
    :class:`~marketplace.order.state_machine.Actor`,
    :class:`~marketplace.order.state_machine.OrderEvent`, and
    :class:`~marketplace.order.state_machine.Guards`.
  * :class:`~marketplace.order.service.OrderService` -- order creation
    (``place_order``, task 10.2); later tasks (12.x/14.x/16.x/17.x) extend this
    class with payment verification cascade, cancellation, modification and
    status queries.
"""

from marketplace.order.service import (
    EMPTY_CART,
    LINE_ALREADY_EXISTS,
    MODIFICATION_STOCK_EXCEEDED,
    MODIFICATION_TRANSITION_SEQ_BASE,
    MODIFY_REQUIRES_SELLER,
    ORDER_MUST_RETAIN_LINE,
    ORDER_NOT_MODIFIABLE,
    PLACEMENT_TRANSITION_SEQ,
    PRE_DECREMENT_STATES,
    PRE_FULFILLMENT_STATES,
    QTY_BELOW_MOQ,
    STATE_TRANSITION_SEQ,
    STOCK_EXCEEDED_AT_PLACEMENT,
    LifecycleResult,
    ModificationOp,
    ModificationResult,
    OrderModification,
    OrderService,
    PlacementResult,
)
from marketplace.order.state_machine import (
    ALLOWED_TRANSITIONS,
    INVALID_TRANSITION,
    NOT_AUTHORIZED_ACTOR,
    NOT_ORDER_OWNER,
    OFFLINE_PAYMENT_NOT_ALLOWED,
    REJECTION_REASON_REQUIRED,
    STOCK_CONFLICT,
    TERMINAL_STATES,
    Actor,
    ActorRole,
    Guards,
    OrderEvent,
    allowed_events,
    is_terminal,
    transition,
)

__all__ = [
    # state machine (task 10.1)
    "transition",
    "Actor",
    "ActorRole",
    "OrderEvent",
    "Guards",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "allowed_events",
    "is_terminal",
    "INVALID_TRANSITION",
    "OFFLINE_PAYMENT_NOT_ALLOWED",
    "STOCK_CONFLICT",
    "REJECTION_REASON_REQUIRED",
    "NOT_ORDER_OWNER",
    "NOT_AUTHORIZED_ACTOR",
    # order service (task 10.2)
    "OrderService",
    "PlacementResult",
    "LifecycleResult",
    "EMPTY_CART",
    "STOCK_EXCEEDED_AT_PLACEMENT",
    "PLACEMENT_TRANSITION_SEQ",
    "STATE_TRANSITION_SEQ",
    # seller order modification (tasks 16.1/16.2)
    "ModificationOp",
    "OrderModification",
    "ModificationResult",
    "PRE_FULFILLMENT_STATES",
    "PRE_DECREMENT_STATES",
    "MODIFY_REQUIRES_SELLER",
    "ORDER_NOT_MODIFIABLE",
    "QTY_BELOW_MOQ",
    "MODIFICATION_STOCK_EXCEEDED",
    "ORDER_MUST_RETAIN_LINE",
    "LINE_ALREADY_EXISTS",
    "MODIFICATION_TRANSITION_SEQ_BASE",
]
