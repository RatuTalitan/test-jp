"""The Order State Machine: a single guarded ``transition`` function (task 10.1).

This module is the **only** place ``orders.state`` is allowed to change
(design.md -> Order State Machine: "All transitions go through one guarded
function; nothing mutates ``orders.state`` directly"). Every other service
(Payment_Service, Admin_Console, Order_Service.place_order/cancel/modify, the
offline-payment path) reaches a state change exclusively by calling
:func:`transition`.

What lives here (task 10.1) vs. later tasks
-------------------------------------------
This module encodes the **state graph** and its **guards** -- the pure decision
of *whether* a ``(state, event, actor, guard)`` edge is legal and, if so, what
the resulting state is. It deliberately does **not** perform the side effects
that *ride on* those transitions; those are implemented by later tasks and are
wired in by the callers that own a transaction:

* the atomic stock decrement on PAYMENT_VERIFIED -> APPROVED (task 12.1),
* the Audit_Trail entry on the offline-approval / modification edges (task 17.x/16.x),
* the customer/seller notifications enqueued per transition (task 18.1).

To keep those behaviours cleanly attachable, the dynamic facts a guard needs
(was the stock check satisfied? is the customer flagged for offline payment? is
the rejection reason valid?) are passed in via the :class:`Guards` context
object rather than recomputed here. A caller that has, for example, already
performed the locked stock re-read (task 12.1) passes ``stock_ok=...``; callers
that don't supply a fact get the safe default. This is the "clean hook" the
later behaviour tasks plug into without changing the graph.

Allowed-transition table (design.md -> Allowed Transition Table)
----------------------------------------------------------------
| From                                   | To               | Event                 | Actor    | Guard                         |
|----------------------------------------|------------------|-----------------------|----------|-------------------------------|
| PLACED                                 | PAYMENT_PENDING  | auto_payment_pending  | System   | -- (auto on creation)         |
| PAYMENT_PENDING                        | PAYMENT_SUBMITTED| submit_utr            | Customer | (UTR validated by Payment_Service) |
| PAYMENT_SUBMITTED                      | PAYMENT_VERIFIED | verify                | Seller   | --                            |
| PAYMENT_PENDING                        | PAYMENT_VERIFIED | offline_verify        | Seller   | customer.offline_payment_allowed |
| PAYMENT_VERIFIED                       | APPROVED         | approve               | Seller   | every line qty <= stock (hook)|
| APPROVED                               | READY_FOR_PICKUP | mark_ready            | Seller   | -- (non-blocking pickup warn) |
| READY_FOR_PICKUP                       | COMPLETED        | mark_collected        | Seller   | --                            |
| PAYMENT_SUBMITTED                      | REJECTED         | reject                | Seller   | reason 1-500 chars            |
| PLACED / PAYMENT_PENDING / PAYMENT_SUBMITTED | CANCELLED  | cancel                | Customer | actor is the placing customer |

Any ``(state, event, actor)`` not represented above is rejected and the state is
left unchanged. In particular there is **no edge out of COMPLETED, CANCELLED, or
REJECTED** (Req 8.7): they are terminal.

Requirements: 6.7, 7.2, 7.7, 8.1, 8.3, 8.4, 8.5, 8.7, 8.8, 9.1, 9.4 (plus the
offline edge 8.9/17.5 whose audit behaviour is task 17.x).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Union

from marketplace.domain.entities import Order, OrderState
from marketplace.domain.results import Conflict, Failure, NotAuthorized, Rejected

__all__ = [
    "ActorRole",
    "Actor",
    "OrderEvent",
    "Guards",
    "TransitionResult",
    "transition",
    "is_terminal",
    "allowed_events",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    # stable status codes
    "INVALID_TRANSITION",
    "OFFLINE_PAYMENT_NOT_ALLOWED",
    "STOCK_CONFLICT",
    "REJECTION_REASON_REQUIRED",
    "NOT_ORDER_OWNER",
    "NOT_AUTHORIZED_ACTOR",
]

# --- Stable status codes (language-agnostic; localized by the Bot_Interface) --
#: No legal edge exists for the ``(state, event)`` pair (includes terminal
#: states and any out-of-sequence attempt). State is left unchanged.
INVALID_TRANSITION = "INVALID_TRANSITION"
#: The offline PAYMENT_PENDING -> PAYMENT_VERIFIED edge was attempted for a
#: customer whose ``offline_payment_allowed`` flag is not set (Req 8.9/17.5).
OFFLINE_PAYMENT_NOT_ALLOWED = "OFFLINE_PAYMENT_NOT_ALLOWED"
#: The PAYMENT_VERIFIED -> APPROVED stock guard failed (Req 7.4). The concrete
#: stock re-read/decrement is task 12.1; this is the structural hook it uses.
STOCK_CONFLICT = "STOCK_CONFLICT"
#: ``reject`` was attempted without a 1-500 character reason (Req 7.5/7.6).
REJECTION_REASON_REQUIRED = "REJECTION_REASON_REQUIRED"
#: ``cancel`` was attempted by a customer who is not the order's owner (Req 9.5).
NOT_ORDER_OWNER = "NOT_ORDER_OWNER"
#: The actor's role is not permitted to trigger the (otherwise legal) edge.
NOT_AUTHORIZED_ACTOR = "NOT_AUTHORIZED_ACTOR"


class ActorRole(str, enum.Enum):
    """Who is performing a transition.

    The state machine matches on the actor's *role* (the ``(state, event,
    actor)`` key of the allowed-transition table). ``SYSTEM`` is the
    Order_Service itself performing the automatic PLACED -> PAYMENT_PENDING edge.
    """

    CUSTOMER = "CUSTOMER"
    SELLER = "SELLER"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class Actor:
    """The principal attempting a transition.

    ``user_id`` is required for ownership-guarded edges (``cancel`` must be
    performed by the placing customer, Req 9.5); it may be ``None`` for the
    ``SYSTEM`` actor that drives the automatic edge.
    """

    role: ActorRole
    user_id: Optional[uuid.UUID] = None

    @classmethod
    def system(cls) -> "Actor":
        """The Order_Service itself (drives the auto PLACED -> PAYMENT_PENDING)."""
        return cls(role=ActorRole.SYSTEM)

    @classmethod
    def customer(cls, user_id: uuid.UUID) -> "Actor":
        return cls(role=ActorRole.CUSTOMER, user_id=user_id)

    @classmethod
    def seller(cls, user_id: Optional[uuid.UUID] = None) -> "Actor":
        return cls(role=ActorRole.SELLER, user_id=user_id)


class OrderEvent(str, enum.Enum):
    """The events that drive the order lifecycle (design.md state diagram)."""

    #: Automatic edge applied by Order_Service immediately after creation.
    AUTO_PAYMENT_PENDING = "auto_payment_pending"
    #: Customer submits a UTR (Payment_Service validates format/uniqueness, 11.x).
    SUBMIT_UTR = "submit_utr"
    #: Seller verifies a submitted payment.
    VERIFY = "verify"
    #: Seller approves an offline-payment order without a UTR (Req 8.9/17.5).
    OFFLINE_VERIFY = "offline_verify"
    #: Seller approval cascade with the atomic stock guard (decrement is 12.1).
    APPROVE = "approve"
    #: Seller marks an approved order ready for pickup.
    MARK_READY = "mark_ready"
    #: Seller marks a ready order collected/completed.
    MARK_COLLECTED = "mark_collected"
    #: Seller rejects a submitted payment with a reason.
    REJECT = "reject"
    #: Placing customer cancels a pre-approval order.
    CANCEL = "cancel"


@dataclass(frozen=True)
class Guards:
    """Dynamic facts the edge guards consult (the "clean hooks", task 10.1).

    Each field is optional with a safe default so a caller only supplies what a
    specific edge needs:

    * ``offline_payment_allowed`` -- the customer's flag, required by the
      offline ``offline_verify`` edge (Req 8.9/17.5).
    * ``stock_ok`` -- result of the caller's locked stock re-read for the
      ``approve`` edge (Req 7.3/7.4). Defaults to ``True`` so the structural
      edge is exercisable now; task 12.1 supplies the real value and the
      ``stock_details`` for the seller-facing conflict message.
    * ``rejection_reason`` -- the 1-500 char reason the ``reject`` edge requires
      (Req 7.5/7.6).
    """

    offline_payment_allowed: bool = False
    stock_ok: bool = True
    stock_details: Optional[Mapping[str, object]] = None
    rejection_reason: Optional[str] = None


@dataclass(frozen=True)
class _Edge:
    """One row of the allowed-transition table."""

    event: OrderEvent
    from_states: frozenset
    to_state: OrderState
    actor_roles: frozenset
    guard: Optional[Callable[[Order, Actor, Guards], Optional[Failure]]] = None


# --------------------------------------------------------------------------- #
# Guard predicates -- each returns ``None`` when the edge may proceed, or a
# typed failure (leaving the state unchanged) when it may not.
# --------------------------------------------------------------------------- #
def _guard_offline(order: Order, actor: Actor, guards: Guards) -> Optional[Failure]:
    if not guards.offline_payment_allowed:
        return Rejected(
            OFFLINE_PAYMENT_NOT_ALLOWED,
            reason="customer is not flagged for offline payment",
            details={"order_id": order.order_id},
        )
    return None


def _guard_approve_stock(order: Order, actor: Actor, guards: Guards) -> Optional[Failure]:
    # Structural hook for the atomic stock check (task 12.1). When the caller
    # has not (yet) performed the locked re-read, ``stock_ok`` defaults True so
    # the edge is exercisable; task 12.1 passes the real outcome + affected
    # lines / available stock in ``stock_details`` (Req 7.4).
    if not guards.stock_ok:
        return Conflict(
            STOCK_CONFLICT,
            reason="one or more line quantities exceed current stock",
            details=dict(guards.stock_details or {}),
        )
    return None


def _guard_reject_reason(order: Order, actor: Actor, guards: Guards) -> Optional[Failure]:
    reason = guards.rejection_reason
    if reason is None or not (1 <= len(reason) <= 500):
        return Rejected(
            REJECTION_REASON_REQUIRED,
            reason="a rejection reason of 1 to 500 characters is required",
            details={"order_id": order.order_id},
        )
    return None


def _guard_cancel_owner(order: Order, actor: Actor, guards: Guards) -> Optional[Failure]:
    # The edge already restricted the actor to CUSTOMER; here we additionally
    # require that the customer is the one who placed the order (Req 9.5).
    if actor.user_id is None or actor.user_id != order.customer_id:
        return NotAuthorized(
            reason="only the placing customer may cancel this order",
            code=NOT_ORDER_OWNER,
        )
    return None


# --------------------------------------------------------------------------- #
# The allowed-transition table (design.md -> Allowed Transition Table).
# This list is the single authoritative encoding of the state graph.
# --------------------------------------------------------------------------- #
ALLOWED_TRANSITIONS: tuple[_Edge, ...] = (
    _Edge(
        event=OrderEvent.AUTO_PAYMENT_PENDING,
        from_states=frozenset({OrderState.PLACED}),
        to_state=OrderState.PAYMENT_PENDING,
        actor_roles=frozenset({ActorRole.SYSTEM}),
    ),
    _Edge(
        event=OrderEvent.SUBMIT_UTR,
        from_states=frozenset({OrderState.PAYMENT_PENDING}),
        to_state=OrderState.PAYMENT_SUBMITTED,
        actor_roles=frozenset({ActorRole.CUSTOMER}),
    ),
    _Edge(
        event=OrderEvent.VERIFY,
        from_states=frozenset({OrderState.PAYMENT_SUBMITTED}),
        to_state=OrderState.PAYMENT_VERIFIED,
        actor_roles=frozenset({ActorRole.SELLER}),
    ),
    _Edge(
        event=OrderEvent.OFFLINE_VERIFY,
        from_states=frozenset({OrderState.PAYMENT_PENDING}),
        to_state=OrderState.PAYMENT_VERIFIED,
        actor_roles=frozenset({ActorRole.SELLER}),
        guard=_guard_offline,
    ),
    _Edge(
        event=OrderEvent.APPROVE,
        from_states=frozenset({OrderState.PAYMENT_VERIFIED}),
        to_state=OrderState.APPROVED,
        actor_roles=frozenset({ActorRole.SELLER}),
        guard=_guard_approve_stock,
    ),
    _Edge(
        event=OrderEvent.MARK_READY,
        from_states=frozenset({OrderState.APPROVED}),
        to_state=OrderState.READY_FOR_PICKUP,
        actor_roles=frozenset({ActorRole.SELLER}),
    ),
    _Edge(
        event=OrderEvent.MARK_COLLECTED,
        from_states=frozenset({OrderState.READY_FOR_PICKUP}),
        to_state=OrderState.COMPLETED,
        actor_roles=frozenset({ActorRole.SELLER}),
    ),
    _Edge(
        event=OrderEvent.REJECT,
        from_states=frozenset({OrderState.PAYMENT_SUBMITTED}),
        to_state=OrderState.REJECTED,
        actor_roles=frozenset({ActorRole.SELLER}),
        guard=_guard_reject_reason,
    ),
    _Edge(
        event=OrderEvent.CANCEL,
        from_states=frozenset(
            {
                OrderState.PLACED,
                OrderState.PAYMENT_PENDING,
                OrderState.PAYMENT_SUBMITTED,
            }
        ),
        to_state=OrderState.CANCELLED,
        actor_roles=frozenset({ActorRole.CUSTOMER}),
        guard=_guard_cancel_owner,
    ),
)

#: The terminal states no edge may leave (Req 8.7).
TERMINAL_STATES: frozenset = frozenset(
    {OrderState.COMPLETED, OrderState.CANCELLED, OrderState.REJECTED}
)

# Index edges by event for O(1) lookup; an event can have several edges keyed by
# distinct ``from_states`` (e.g. ``cancel`` from three states).
_EDGES_BY_EVENT: dict = {}
for _edge in ALLOWED_TRANSITIONS:
    _EDGES_BY_EVENT.setdefault(_edge.event, []).append(_edge)


#: A transition returns the (mutated) order on success, or a typed failure.
TransitionResult = Union[Order, Failure]


def is_terminal(state: OrderState) -> bool:
    """True if ``state`` is COMPLETED/CANCELLED/REJECTED (no edge leaves it)."""
    return state in TERMINAL_STATES


def allowed_events(state: OrderState) -> set:
    """Return the set of :class:`OrderEvent` legally applicable from ``state``.

    Useful for tests and for the Admin_Console to render only valid actions.
    Terminal states return an empty set.
    """
    return {
        edge.event
        for edge in ALLOWED_TRANSITIONS
        if state in edge.from_states
    }


def transition(
    order: Order,
    event: "OrderEvent | str",
    actor: Actor,
    guards: Optional[Guards] = None,
) -> TransitionResult:
    """Apply ``event`` to ``order`` on behalf of ``actor`` -- the only mutator.

    This is the single guarded gateway through which ``order.state`` ever
    changes (design.md -> Order State Machine). It consults
    :data:`ALLOWED_TRANSITIONS`:

    1. If no edge exists for ``(order.state, event)`` -- including any event on a
       terminal order -- the state is left unchanged and a state-preserving
       :class:`~marketplace.domain.results.Rejected` (``INVALID_TRANSITION``) is
       returned (Req 8.7/8.8).
    2. If an edge exists but ``actor.role`` is not permitted, a
       :class:`~marketplace.domain.results.NotAuthorized` is returned and the
       state is unchanged.
    3. If the edge's guard rejects (offline flag absent, stock conflict,
       missing rejection reason, non-owner cancel), the corresponding typed
       failure is returned and the state is unchanged.
    4. Otherwise ``order.state`` is advanced to the edge's target state and the
       (mutated) order is returned.

    Side effects that *ride on* a transition (stock decrement, audit entries,
    notifications) are intentionally **not** performed here; the caller that owns
    the transaction performs them after a successful transition (tasks
    12.x/16.x/17.x/18.x). The only intrinsic write made here beyond ``state`` is
    recording the validated ``rejection_reason`` on the REJECTED edge, since it
    is part of forming a valid rejected order.

    Args:
        order: the order to transition (mutated in place on success).
        event: an :class:`OrderEvent` (or its string value).
        actor: the principal performing the action.
        guards: dynamic facts the edge guard consults; defaults are safe.

    Returns:
        The mutated :class:`~marketplace.domain.entities.Order` on success, or a
        typed failure leaving ``order`` untouched.
    """
    guards = guards if guards is not None else Guards()

    # Normalize a string event to the enum; an unknown event is an illegal
    # transition (state-preserving), never an exception.
    if not isinstance(event, OrderEvent):
        try:
            event = OrderEvent(event)
        except ValueError:
            return Rejected(
                INVALID_TRANSITION,
                reason=f"unknown event {event!r}",
                details={"state": order.state.value, "event": str(event)},
            )

    candidates = _EDGES_BY_EVENT.get(event, [])
    edge = next((e for e in candidates if order.state in e.from_states), None)
    if edge is None:
        return Rejected(
            INVALID_TRANSITION,
            reason=(
                f"event {event.value} is not allowed from state "
                f"{order.state.value}"
            ),
            details={"state": order.state.value, "event": event.value},
        )

    if actor.role not in edge.actor_roles:
        return NotAuthorized(
            reason=(
                f"actor role {actor.role.value} may not perform "
                f"{event.value} from {order.state.value}"
            ),
            code=NOT_AUTHORIZED_ACTOR,
        )

    if edge.guard is not None:
        failure = edge.guard(order, actor, guards)
        if failure is not None:
            return failure

    # All checks passed: advance the state (the sole place this ever happens).
    order.state = edge.to_state
    if event is OrderEvent.REJECT:
        # The reason was validated by the guard above; record it as part of
        # forming a valid REJECTED order (Req 7.5).
        order.rejection_reason = guards.rejection_reason
    return order
