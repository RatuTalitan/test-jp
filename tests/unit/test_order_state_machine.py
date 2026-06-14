"""Unit tests for the Order State Machine (task 10.1).

Exercise the single guarded ``transition`` function against the design's
allowed-transition table: the happy-path edges, the offline edge and its guard,
actor-role gating, ownership-guarded cancellation, terminal-state immutability
(no edge out of COMPLETED/CANCELLED/REJECTED), and the structural guards for the
approve (stock) and reject (reason) edges.

Requirements: 6.7, 7.2, 7.7, 8.1, 8.3, 8.4, 8.5, 8.7, 8.8, 9.1, 9.4.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from marketplace.domain.entities import Order, OrderState, new_id
from marketplace.domain.results import Conflict, NotAuthorized, Rejected
from marketplace.order.state_machine import (
    INVALID_TRANSITION,
    NOT_AUTHORIZED_ACTOR,
    NOT_ORDER_OWNER,
    OFFLINE_PAYMENT_NOT_ALLOWED,
    REJECTION_REASON_REQUIRED,
    STOCK_CONFLICT,
    TERMINAL_STATES,
    Actor,
    Guards,
    OrderEvent,
    allowed_events,
    is_terminal,
    transition,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_order(state: OrderState, customer_id: uuid.UUID | None = None) -> Order:
    return Order(
        order_id=new_id(),
        customer_id=customer_id or new_id(),
        state=state,
        total_amount=Decimal("0.00"),
        items=[],
    )


SELLER = Actor.seller()


# --------------------------------------------------------------------------- #
# Happy-path allowed edges (Req 8.8 sequence)
# --------------------------------------------------------------------------- #
def test_auto_edge_placed_to_payment_pending():
    order = make_order(OrderState.PLACED)
    result = transition(order, OrderEvent.AUTO_PAYMENT_PENDING, Actor.system())
    assert result is order
    assert order.state is OrderState.PAYMENT_PENDING


def test_submit_utr_payment_pending_to_submitted():
    customer = new_id()
    order = make_order(OrderState.PAYMENT_PENDING, customer)
    result = transition(order, OrderEvent.SUBMIT_UTR, Actor.customer(customer))
    assert isinstance(result, Order)
    assert order.state is OrderState.PAYMENT_SUBMITTED


def test_verify_submitted_to_verified():
    order = make_order(OrderState.PAYMENT_SUBMITTED)
    transition(order, OrderEvent.VERIFY, SELLER)
    assert order.state is OrderState.PAYMENT_VERIFIED


def test_approve_verified_to_approved_with_stock_ok():
    order = make_order(OrderState.PAYMENT_VERIFIED)
    transition(order, OrderEvent.APPROVE, SELLER, Guards(stock_ok=True))
    assert order.state is OrderState.APPROVED


def test_mark_ready_then_collected():
    order = make_order(OrderState.APPROVED)
    transition(order, OrderEvent.MARK_READY, SELLER)
    assert order.state is OrderState.READY_FOR_PICKUP
    transition(order, OrderEvent.MARK_COLLECTED, SELLER)
    assert order.state is OrderState.COMPLETED


def test_full_happy_path_sequence():
    customer = new_id()
    order = make_order(OrderState.PLACED, customer)
    transition(order, OrderEvent.AUTO_PAYMENT_PENDING, Actor.system())
    transition(order, OrderEvent.SUBMIT_UTR, Actor.customer(customer))
    transition(order, OrderEvent.VERIFY, SELLER)
    transition(order, OrderEvent.APPROVE, SELLER, Guards(stock_ok=True))
    transition(order, OrderEvent.MARK_READY, SELLER)
    transition(order, OrderEvent.MARK_COLLECTED, SELLER)
    assert order.state is OrderState.COMPLETED


# --------------------------------------------------------------------------- #
# Offline edge + guard (Req 8.9 / 17.5)
# --------------------------------------------------------------------------- #
def test_offline_verify_allowed_when_flagged():
    order = make_order(OrderState.PAYMENT_PENDING)
    transition(
        order,
        OrderEvent.OFFLINE_VERIFY,
        SELLER,
        Guards(offline_payment_allowed=True),
    )
    assert order.state is OrderState.PAYMENT_VERIFIED


def test_offline_verify_rejected_when_not_flagged_state_unchanged():
    order = make_order(OrderState.PAYMENT_PENDING)
    result = transition(
        order,
        OrderEvent.OFFLINE_VERIFY,
        SELLER,
        Guards(offline_payment_allowed=False),
    )
    assert isinstance(result, Rejected)
    assert result.code == OFFLINE_PAYMENT_NOT_ALLOWED
    assert order.state is OrderState.PAYMENT_PENDING  # unchanged


# --------------------------------------------------------------------------- #
# Reject edge + reason guard (Req 7.5 / 7.6)
# --------------------------------------------------------------------------- #
def test_reject_records_reason():
    order = make_order(OrderState.PAYMENT_SUBMITTED)
    transition(
        order, OrderEvent.REJECT, SELLER, Guards(rejection_reason="payment not received")
    )
    assert order.state is OrderState.REJECTED
    assert order.rejection_reason == "payment not received"


@pytest.mark.parametrize("reason", [None, "", "x" * 501])
def test_reject_requires_valid_reason(reason):
    order = make_order(OrderState.PAYMENT_SUBMITTED)
    result = transition(order, OrderEvent.REJECT, SELLER, Guards(rejection_reason=reason))
    assert isinstance(result, Rejected)
    assert result.code == REJECTION_REASON_REQUIRED
    assert order.state is OrderState.PAYMENT_SUBMITTED  # unchanged


# --------------------------------------------------------------------------- #
# Approve stock guard hook (Req 7.4)
# --------------------------------------------------------------------------- #
def test_approve_blocked_on_stock_conflict_state_unchanged():
    order = make_order(OrderState.PAYMENT_VERIFIED)
    result = transition(
        order,
        OrderEvent.APPROVE,
        SELLER,
        Guards(stock_ok=False, stock_details={"lines": []}),
    )
    assert isinstance(result, Conflict)
    assert result.code == STOCK_CONFLICT
    assert order.state is OrderState.PAYMENT_VERIFIED  # unchanged


# --------------------------------------------------------------------------- #
# Cancel edge + ownership guard (Req 9.1 / 9.4 / 9.5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [OrderState.PLACED, OrderState.PAYMENT_PENDING, OrderState.PAYMENT_SUBMITTED],
)
def test_cancel_allowed_for_owner_in_pre_approval_states(state):
    customer = new_id()
    order = make_order(state, customer)
    transition(order, OrderEvent.CANCEL, Actor.customer(customer))
    assert order.state is OrderState.CANCELLED


def test_cancel_rejected_for_non_owner_state_unchanged():
    owner = new_id()
    other = new_id()
    order = make_order(OrderState.PLACED, owner)
    result = transition(order, OrderEvent.CANCEL, Actor.customer(other))
    assert isinstance(result, NotAuthorized)
    assert result.code == NOT_ORDER_OWNER
    assert order.state is OrderState.PLACED  # unchanged


@pytest.mark.parametrize(
    "state",
    [OrderState.APPROVED, OrderState.READY_FOR_PICKUP],
)
def test_cancel_rejected_in_non_cancellable_states(state):
    customer = new_id()
    order = make_order(state, customer)
    result = transition(order, OrderEvent.CANCEL, Actor.customer(customer))
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is state  # unchanged


# --------------------------------------------------------------------------- #
# Actor-role gating
# --------------------------------------------------------------------------- #
def test_customer_cannot_verify():
    order = make_order(OrderState.PAYMENT_SUBMITTED)
    result = transition(order, OrderEvent.VERIFY, Actor.customer(new_id()))
    assert isinstance(result, NotAuthorized)
    assert result.code == NOT_AUTHORIZED_ACTOR
    assert order.state is OrderState.PAYMENT_SUBMITTED  # unchanged


def test_seller_cannot_submit_utr():
    order = make_order(OrderState.PAYMENT_PENDING)
    result = transition(order, OrderEvent.SUBMIT_UTR, SELLER)
    assert isinstance(result, NotAuthorized)
    assert result.code == NOT_AUTHORIZED_ACTOR
    assert order.state is OrderState.PAYMENT_PENDING  # unchanged


# --------------------------------------------------------------------------- #
# Out-of-sequence / illegal edges leave state unchanged (Req 8.8)
# --------------------------------------------------------------------------- #
def test_verify_not_allowed_from_payment_pending():
    order = make_order(OrderState.PAYMENT_PENDING)
    result = transition(order, OrderEvent.VERIFY, SELLER)
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is OrderState.PAYMENT_PENDING


def test_mark_ready_only_from_approved():
    order = make_order(OrderState.PAYMENT_VERIFIED)
    result = transition(order, OrderEvent.MARK_READY, SELLER)
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is OrderState.PAYMENT_VERIFIED


def test_mark_collected_only_from_ready():
    order = make_order(OrderState.APPROVED)
    result = transition(order, OrderEvent.MARK_COLLECTED, SELLER)
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is OrderState.APPROVED


def test_unknown_string_event_is_invalid_transition():
    order = make_order(OrderState.PLACED)
    result = transition(order, "fly_to_moon", Actor.system())
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is OrderState.PLACED


# --------------------------------------------------------------------------- #
# Terminal-state immutability: NO edge leaves COMPLETED/CANCELLED/REJECTED (8.7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("event", list(OrderEvent))
def test_no_transition_out_of_terminal_states(state, event):
    customer = new_id()
    order = make_order(state, customer)
    # Provide the most permissive guards possible; the edge must still not exist.
    result = transition(
        order,
        event,
        Actor.customer(customer),
        Guards(
            offline_payment_allowed=True,
            stock_ok=True,
            rejection_reason="reason",
        ),
    )
    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    assert order.state is state  # immutable


def test_is_terminal_and_allowed_events_consistency():
    for state in TERMINAL_STATES:
        assert is_terminal(state)
        assert allowed_events(state) == set()
    # A non-terminal state has at least one allowed event.
    assert OrderEvent.SUBMIT_UTR in allowed_events(OrderState.PAYMENT_PENDING)
    assert not is_terminal(OrderState.PLACED)
