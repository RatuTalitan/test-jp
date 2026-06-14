"""Unit tests for the Notification_Service (tasks 18.1 + 18.2).

Exercised against the in-memory Unit-of-Work with a fake
:class:`~marketplace.notification.sender.MessageSender` and a fully controllable
``now``:

* **Enqueue (task 18.1, Req 11.1/11.2):** writes a ``PENDING`` row and is
  idempotent on ``(order_id, kind, transition_seq)`` (re-enqueue returns the
  existing row, never a duplicate); the customer state-change and seller
  new-order helpers build the expected payloads.
* **Delivery loop (task 18.2, Req 11.3/11.4/11.5):** success marks ``DELIVERED``
  and resolves a prior failure; failure records the failure, increments
  ``attempts`` and pushes ``next_attempt_at`` >= 30s out without yet abandoning;
  exhausting the 4-attempt budget marks ``UNDELIVERED`` and leaves the order's
  state untouched; and only *due* rows are processed.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from marketplace.domain.entities import (
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderState,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.notification.sender import MessageSender, SendResult
from marketplace.notification.service import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    NotificationService,
)

BASE = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def uow(db: InMemoryDatabase) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(db)


class FakeSender:
    """A configurable, call-recording :class:`MessageSender` for tests.

    ``outcomes`` is an iterable of results consumed one per ``send`` call; when
    exhausted it falls back to ``default``. Each element may be a ``bool`` or a
    :class:`SendResult`. ``calls`` records every ``(recipient_id, payload)``.
    """

    def __init__(self, outcomes=None, default=True):
        self._outcomes = list(outcomes or [])
        self._default = default
        self.calls: list = []

    def send(self, recipient_id, payload):
        self.calls.append((recipient_id, payload))
        if self._outcomes:
            return self._outcomes.pop(0)
        return self._default


def make_notification(uow, *, order_id=None, recipient_id=None, **overrides):
    """Insert a PENDING notification row directly and return it."""
    notification = Notification(
        notification_id=new_id(),
        order_id=order_id or new_id(),
        recipient_id=recipient_id or new_id(),
        kind=NotificationKind.STATE_CHANGE,
        transition_seq=overrides.pop("transition_seq", 1),
        payload=overrides.pop(
            "payload",
            {"format_version": 1, "kind": "STATE_CHANGE", "order_id": "x"},
        ),
        status=overrides.pop("status", NotificationStatus.PENDING),
        attempts=overrides.pop("attempts", 0),
        next_attempt_at=overrides.pop("next_attempt_at", None),
        created_at=overrides.pop("created_at", BASE),
        **overrides,
    )
    return uow.notifications.add(notification)


def make_order(uow, *, state=OrderState.PAYMENT_PENDING) -> Order:
    order = Order(
        order_id=new_id(),
        customer_id=new_id(),
        state=state,
        total_amount=Decimal("10.00"),
        created_at=BASE,
    )
    return uow.orders.add(order)


# =========================================================================== #
# Task 18.1 -- enqueue (Req 11.1, 11.2)
# =========================================================================== #
def test_enqueue_writes_pending_row(uow):
    svc = NotificationService(uow)
    order_id, recipient_id = new_id(), new_id()

    notif = svc.enqueue(
        order_id,
        recipient_id,
        NotificationKind.STATE_CHANGE,
        {"kind": "STATE_CHANGE", "order_id": str(order_id)},
        transition_seq=3,
    )

    assert notif.status is NotificationStatus.PENDING
    assert notif.attempts == 0
    assert notif.order_id == order_id
    assert notif.recipient_id == recipient_id
    assert notif.transition_seq == 3
    # Payload is normalised to carry a format_version (Req 13.7).
    assert notif.payload["format_version"] == 1
    # Actually persisted.
    assert uow.notifications.get(notif.notification_id) is not None


def test_enqueue_is_idempotent_on_identity_key(uow):
    svc = NotificationService(uow)
    order_id, recipient_id = new_id(), new_id()

    first = svc.enqueue(
        order_id, recipient_id, NotificationKind.STATE_CHANGE, {"a": 1}, 1
    )
    # Re-enqueue with the same (order_id, kind, transition_seq) -> same row.
    second = svc.enqueue(
        order_id, recipient_id, NotificationKind.STATE_CHANGE, {"a": 2}, 1
    )

    assert second.notification_id == first.notification_id
    # No duplicate row was written.
    rows = [
        n
        for n in uow.notifications.list_due(BASE + timedelta(days=1))
        if n.order_id == order_id
    ]
    assert len(rows) == 1
    # The original payload is preserved (the second call did not overwrite it).
    assert uow.notifications.get(first.notification_id).payload == first.payload


def test_enqueue_distinct_transition_seq_creates_distinct_rows(uow):
    svc = NotificationService(uow)
    order_id, recipient_id = new_id(), new_id()

    n1 = svc.enqueue(order_id, recipient_id, NotificationKind.STATE_CHANGE, {}, 1)
    n2 = svc.enqueue(order_id, recipient_id, NotificationKind.STATE_CHANGE, {}, 2)

    assert n1.notification_id != n2.notification_id


def test_enqueue_customer_state_change_payload(uow):
    svc = NotificationService(uow)
    order_id, customer_id = new_id(), new_id()

    notif = svc.enqueue_customer_state_change(
        order_id,
        customer_id,
        OrderState.APPROVED,
        transition_seq=5,
        extra={"reason": "ok"},
    )

    assert notif.kind is NotificationKind.STATE_CHANGE
    assert notif.recipient_id == customer_id
    assert notif.payload["order_id"] == str(order_id)
    assert notif.payload["new_state"] == OrderState.APPROVED.value
    assert notif.payload["reason"] == "ok"


def test_enqueue_seller_new_order_payload(uow):
    svc = NotificationService(uow)
    order_id, seller_id = new_id(), new_id()

    notif = svc.enqueue_seller_new_order(
        order_id,
        seller_id,
        transition_seq=1,
        new_state=OrderState.PAYMENT_PENDING,
        total_amount=Decimal("31.00"),
    )

    assert notif.kind is NotificationKind.NEW_ORDER
    assert notif.recipient_id == seller_id
    assert notif.payload["order_id"] == str(order_id)
    assert notif.payload["new_state"] == OrderState.PAYMENT_PENDING.value
    assert notif.payload["total_amount"] == "31.00"


# =========================================================================== #
# Task 18.2 -- delivery loop success path (Req 11.5)
# =========================================================================== #
def test_deliver_pending_success_marks_delivered(uow):
    sender = FakeSender(default=True)
    svc = NotificationService(uow, sender)
    notif = make_notification(uow)

    report = svc.deliver_pending(BASE)

    stored = uow.notifications.get(notif.notification_id)
    assert stored.status is NotificationStatus.DELIVERED
    assert stored.attempts == 1
    assert stored.last_attempt_at == BASE
    assert stored.next_attempt_at is None
    assert report.delivered == [notif.notification_id]
    # First-try success had no prior failure to resolve.
    assert report.resolved == []
    assert len(sender.calls) == 1


def test_deliver_pending_success_after_failure_resolves_prior_failure(uow):
    # First attempt fails, second (later) attempt succeeds (Req 11.5).
    sender = FakeSender(outcomes=[SendResult.fail("boom"), SendResult.ok()])
    svc = NotificationService(uow, sender)
    notif = make_notification(uow)

    # Tick 1: failure -> FAILED, next_attempt_at = BASE + 30s.
    svc.deliver_pending(BASE)
    after_fail = uow.notifications.get(notif.notification_id)
    assert after_fail.status is NotificationStatus.FAILED
    assert after_fail.attempts == 1

    # Tick 2 at the scheduled time: success -> DELIVERED + failure resolved.
    later = BASE + timedelta(seconds=RETRY_BACKOFF_SECONDS)
    report = svc.deliver_pending(later)

    stored = uow.notifications.get(notif.notification_id)
    assert stored.status is NotificationStatus.DELIVERED
    assert stored.attempts == 2
    assert report.delivered == [notif.notification_id]
    assert report.resolved == [notif.notification_id]
    # The recorded failure is flagged resolved on the payload (Req 11.5).
    assert stored.payload["delivery"]["resolved"] is True
    assert "resolved_at" in stored.payload["delivery"]


# =========================================================================== #
# Task 18.2 -- failure path records + backs off (Req 11.3)
# =========================================================================== #
def test_deliver_pending_failure_records_and_backs_off(uow):
    sender = FakeSender(default=SendResult.fail("network"))
    svc = NotificationService(uow, sender)
    order = make_order(uow)
    recipient_id = new_id()
    notif = make_notification(uow, order_id=order.order_id, recipient_id=recipient_id)

    report = svc.deliver_pending(BASE)

    stored = uow.notifications.get(notif.notification_id)
    # Failure recorded, attempts incremented, not yet undelivered (Req 11.3).
    assert stored.status is NotificationStatus.FAILED
    assert stored.attempts == 1
    assert stored.last_attempt_at == BASE
    # next_attempt_at advanced by exactly >= 30s (Req 11.3).
    assert stored.next_attempt_at == BASE + timedelta(seconds=RETRY_BACKOFF_SECONDS)
    assert (stored.next_attempt_at - BASE).total_seconds() >= 30
    # Failure record carries the order id + intended recipient (Req 11.3).
    failure = stored.payload["delivery"]["failures"][-1]
    assert failure["order_id"] == str(order.order_id)
    assert failure["recipient_id"] == str(recipient_id)
    assert failure["detail"] == "network"
    assert report.failed == [notif.notification_id]
    assert report.undelivered == []


def test_failed_row_not_due_until_backoff_elapses(uow):
    # Enforces the >= 30s spacing between consecutive attempts (Req 11.3).
    sender = FakeSender(default=SendResult.fail())
    svc = NotificationService(uow, sender)
    notif = make_notification(uow)

    svc.deliver_pending(BASE)  # attempt 1 -> FAILED, next = BASE + 30s
    assert len(sender.calls) == 1

    # A tick before the backoff elapses does NOT reprocess the row.
    report_early = svc.deliver_pending(BASE + timedelta(seconds=29))
    assert report_early.processed == 0
    assert len(sender.calls) == 1
    assert uow.notifications.get(notif.notification_id).attempts == 1

    # A tick at the scheduled time does reprocess it.
    svc.deliver_pending(BASE + timedelta(seconds=RETRY_BACKOFF_SECONDS))
    assert len(sender.calls) == 2
    assert uow.notifications.get(notif.notification_id).attempts == 2


# =========================================================================== #
# Task 18.2 -- exhaustion -> UNDELIVERED, order untouched (Req 11.4)
# =========================================================================== #
def test_deliver_pending_exhausts_after_four_attempts(uow):
    sender = FakeSender(default=SendResult.fail("down"))
    svc = NotificationService(uow, sender)
    order = make_order(uow, state=OrderState.PAYMENT_PENDING)
    notif = make_notification(uow, order_id=order.order_id)

    # Drive 4 total attempts (1 + 3 retries), each at its due time.
    now = BASE
    for expected_attempts in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        svc.deliver_pending(now)
        stored = uow.notifications.get(notif.notification_id)
        assert stored.attempts == expected_attempts
        now = now + timedelta(seconds=RETRY_BACKOFF_SECONDS)

    # After the 4th failed attempt the row is abandoned (Req 11.4).
    stored = uow.notifications.get(notif.notification_id)
    assert stored.status is NotificationStatus.UNDELIVERED
    assert stored.attempts == MAX_DELIVERY_ATTEMPTS
    assert stored.next_attempt_at is None
    assert len(sender.calls) == MAX_DELIVERY_ATTEMPTS

    # The order's state is left completely unchanged (Req 11.4).
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING

    # A terminal (UNDELIVERED) row is never reprocessed again.
    report = svc.deliver_pending(now + timedelta(days=1))
    assert report.processed == 0
    assert len(sender.calls) == MAX_DELIVERY_ATTEMPTS


# =========================================================================== #
# Task 18.2 -- only due rows are processed
# =========================================================================== #
def test_deliver_pending_processes_only_due_rows(uow):
    sender = FakeSender(default=True)
    svc = NotificationService(uow, sender)

    due_pending = make_notification(uow)  # PENDING, no next_attempt_at -> due
    due_failed = make_notification(
        uow, status=NotificationStatus.FAILED, attempts=1, next_attempt_at=BASE
    )
    future_failed = make_notification(
        uow,
        status=NotificationStatus.FAILED,
        attempts=1,
        next_attempt_at=BASE + timedelta(seconds=60),
    )
    already_delivered = make_notification(
        uow, status=NotificationStatus.DELIVERED, attempts=1
    )
    undelivered = make_notification(
        uow, status=NotificationStatus.UNDELIVERED, attempts=MAX_DELIVERY_ATTEMPTS
    )

    report = svc.deliver_pending(BASE)

    delivered = set(report.delivered)
    assert delivered == {due_pending.notification_id, due_failed.notification_id}
    # Not-due / terminal rows are untouched.
    assert uow.notifications.get(future_failed.notification_id).status is (
        NotificationStatus.FAILED
    )
    assert uow.notifications.get(future_failed.notification_id).attempts == 1
    assert uow.notifications.get(already_delivered.notification_id).attempts == 1
    assert uow.notifications.get(undelivered.notification_id).status is (
        NotificationStatus.UNDELIVERED
    )
    assert len(sender.calls) == 2


def test_deliver_pending_without_sender_raises(uow):
    svc = NotificationService(uow)  # no sender supplied
    with pytest.raises(RuntimeError):
        svc.deliver_pending(BASE)


# =========================================================================== #
# Port contract sanity
# =========================================================================== #
def test_fake_sender_satisfies_message_sender_protocol():
    assert isinstance(FakeSender(), MessageSender)


def test_sender_exception_is_treated_as_failure(uow):
    class RaisingSender:
        def send(self, recipient_id, payload):
            raise RuntimeError("transport exploded")

    svc = NotificationService(uow, RaisingSender())
    notif = make_notification(uow)

    report = svc.deliver_pending(BASE)

    stored = uow.notifications.get(notif.notification_id)
    assert stored.status is NotificationStatus.FAILED
    assert stored.attempts == 1
    assert report.failed == [notif.notification_id]
