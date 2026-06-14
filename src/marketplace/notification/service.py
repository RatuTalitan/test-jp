"""Notification_Service: transactional enqueue + bounded retry worker (task 18).

This is the channel- and DB-agnostic Notification_Service from design.md ->
``Notification_Service``. Like the other services it reaches persistence **only**
through the repository protocols on a
:class:`~marketplace.domain.repositories.UnitOfWork` and never imports the ORM
or opens a connection itself (design.md -> Architectural Principles). It is also
**language-agnostic**: a notification stores a ``{format_version, ...}`` JSON
payload of stable codes + data placeholders, and the Bot_Interface renders that
into localized prose for the recipient's active language (Req 18).

Two responsibilities, matching the two sub-tasks:

* **Transactional enqueue (task 18.1, Req 11.1/11.2).** :meth:`enqueue` writes a
  ``PENDING`` ``notifications`` row in the **same transaction** as the caller's
  state change, keyed idempotently by ``(order_id, kind, transition_seq)`` so a
  re-enqueue (e.g. a retried transition) returns the *existing* row and never
  duplicates the logical message (design.md -> Notifications: idempotency). Two
  convenience helpers build the standard payloads:
  :meth:`enqueue_customer_state_change` (the per-transition customer message
  identifying the order and its new state, Req 11.1) and
  :meth:`enqueue_seller_new_order` (the seller "new order awaits payment"
  message, Req 11.2).

* **Retry worker delivery loop (task 18.2, Req 11.3/11.4/11.5).**
  :meth:`deliver_pending` polls due rows
  (``status in {PENDING, FAILED} AND next_attempt_at <= now``) via
  ``uow.notifications.list_due`` and attempts delivery through an injected
  :class:`~marketplace.notification.sender.MessageSender` (the seam the
  Bot_Interface wires to Telegram in task 19). On success the row is marked
  ``DELIVERED`` and any prior recorded failure is resolved (Req 11.5); on
  failure the failure is recorded with the order id + recipient, ``attempts`` is
  incremented and ``next_attempt_at`` is pushed to ``now + 30s`` (Req 11.3);
  after **4 total attempts** (the first try + 3 retries) still failing the row
  is marked ``UNDELIVERED`` and the order's state is left untouched (Req 11.4).
  Spacing retries via ``next_attempt_at`` guarantees consecutive attempts are
  >= 30s apart (Req 11.3).

The caller owns the transaction boundary (one ``UnitOfWork`` per unit of work):
enqueue participates in the caller's open transaction, and a retry-worker tick
opens a ``UnitOfWork``, calls :meth:`deliver_pending`, and commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from marketplace.domain.entities import (
    Notification,
    NotificationKind,
    NotificationStatus,
    new_id,
)
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.serialization import FORMAT_VERSION, ensure_versioned_document
from marketplace.notification.sender import MessageSender, SendResult, as_send_result

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "DeliveryReport",
    "NotificationService",
]

#: Total delivery attempts permitted before a notification is abandoned: the
#: first attempt plus the 3 additional retries mandated by Req 11.3 = 4.
MAX_DELIVERY_ATTEMPTS = 4

#: Minimum spacing between consecutive delivery attempts (Req 11.3: ">= 30s").
#: A failed attempt sets ``next_attempt_at = now + 30s`` so the row is not due
#: again until at least this many seconds have elapsed.
RETRY_BACKOFF_SECONDS = 30


class DeliveryReport:
    """A summary of one :meth:`NotificationService.deliver_pending` tick.

    Holds the ids of the notifications processed in this tick, bucketed by
    outcome, so a caller (and the unit tests) can assert what happened without
    re-querying. ``resolved`` lists rows that delivered *after* one or more
    prior failures -- the Req 11.5 "failure resolved" case (a subset of
    ``delivered``).
    """

    def __init__(self) -> None:
        self.delivered: list = []
        self.failed: list = []
        self.undelivered: list = []
        self.resolved: list = []

    @property
    def processed(self) -> int:
        """Total notifications acted on in this tick."""
        return len(self.delivered) + len(self.failed) + len(self.undelivered)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            "DeliveryReport("
            f"processed={self.processed}, delivered={len(self.delivered)}, "
            f"failed={len(self.failed)}, undelivered={len(self.undelivered)}, "
            f"resolved={len(self.resolved)})"
        )


class NotificationService:
    """Notification operations bound to a single :class:`UnitOfWork`.

    Constructed with the active ``UnitOfWork`` and, for delivery, a
    :class:`~marketplace.notification.sender.MessageSender`. Enqueue methods need
    only the ``UnitOfWork``; :meth:`deliver_pending` requires a sender. Mutating
    operations leave the transaction open for the caller to commit.
    """

    def __init__(self, uow: UnitOfWork, sender: Optional[MessageSender] = None) -> None:
        self._uow = uow
        self._sender = sender

    # ===================================================================== #
    # Task 18.1 -- transactional, idempotent enqueue (Req 11.1, 11.2)
    # ===================================================================== #
    def enqueue(
        self,
        order_id,
        recipient_id,
        kind: NotificationKind,
        payload: Mapping[str, Any],
        transition_seq: int,
    ) -> Notification:
        """Enqueue a ``PENDING`` notification, idempotent per identity key.

        The row is written through ``uow.notifications`` in the **caller's open
        transaction**, so the notification commits atomically with the state
        change that triggered it (Req 11.1/11.2) -- there is no window in which
        the order moved but the message was lost, or vice versa.

        Idempotency: if a row already exists for
        ``(order_id, kind, transition_seq)`` the **existing** row is returned
        unchanged and no second row is written (design.md -> Notifications). This
        makes the enqueue safe to call again on a retried transition: the seller
        / customer never receives a duplicate logical message.

        The ``payload`` is normalised to carry ``format_version`` (Req 13.7) via
        :func:`~marketplace.domain.serialization.ensure_versioned_document`.
        """
        existing = self._uow.notifications.find_by_idempotency_key(
            order_id, kind, transition_seq
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order_id,
            recipient_id=recipient_id,
            kind=kind,
            transition_seq=transition_seq,
            payload=dict(ensure_versioned_document(dict(payload))),
            status=NotificationStatus.PENDING,
            attempts=0,
        )
        return self._uow.notifications.add(notification)

    def enqueue_customer_state_change(
        self,
        order_id,
        customer_id,
        new_state,
        transition_seq: int,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Notification:
        """Enqueue the per-transition customer state-change message (Req 11.1).

        Builds the standard ``STATE_CHANGE`` payload identifying the order and
        its new ``Order_State`` (the two facts Req 11.1 requires the message to
        carry), then delegates to :meth:`enqueue` for the idempotent write. The
        per-order ``transition_seq`` is the monotonic event counter the caller
        (Order_Service) supplies for each transition, so each distinct
        transition enqueues exactly one customer notification.

        ``new_state`` accepts an ``OrderState`` enum or its string value; any
        ``extra`` keys are merged into the payload for richer rendering (e.g. a
        rejection reason or pickup location) without changing the stable shape.
        """
        payload: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "kind": NotificationKind.STATE_CHANGE.value,
            "order_id": str(order_id),
            "new_state": getattr(new_state, "value", new_state),
        }
        if extra:
            payload.update(dict(extra))
        return self.enqueue(
            order_id,
            customer_id,
            NotificationKind.STATE_CHANGE,
            payload,
            transition_seq,
        )

    def enqueue_seller_new_order(
        self,
        order_id,
        seller_id,
        transition_seq: int,
        *,
        new_state=None,
        total_amount=None,
    ) -> Notification:
        """Enqueue the seller "new order awaits payment" message (Req 11.2).

        Builds the standard ``NEW_ORDER`` payload identifying the order (Req
        11.2) and delegates to :meth:`enqueue`. ``new_state`` and
        ``total_amount`` are optional placeholders included for the rendered
        message; they accept enums/Decimals and are stringified into the JSON
        document. The payload shape matches the one Order_Service already writes
        at placement, so the two enqueue paths are interchangeable.
        """
        payload: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "kind": NotificationKind.NEW_ORDER.value,
            "order_id": str(order_id),
        }
        if new_state is not None:
            payload["new_state"] = getattr(new_state, "value", new_state)
        if total_amount is not None:
            payload["total_amount"] = str(total_amount)
        return self.enqueue(
            order_id,
            seller_id,
            NotificationKind.NEW_ORDER,
            payload,
            transition_seq,
        )

    # ===================================================================== #
    # Task 18.2 -- the retry worker delivery loop (Req 11.3, 11.4, 11.5)
    # ===================================================================== #
    def deliver_pending(self, now: datetime) -> DeliveryReport:
        """Attempt delivery of every notification due at ``now`` (Req 11.3-11.5).

        A row is **due** when ``status in {PENDING, FAILED}`` and its
        ``next_attempt_at`` is unset or ``<= now`` (``uow.notifications.list_due``
        encodes exactly this). Already-``DELIVERED`` / ``UNDELIVERED`` rows are
        terminal and never reprocessed, and a ``FAILED`` row whose
        ``next_attempt_at`` is still in the future is **not** due -- this is how
        the >= 30s spacing between attempts is enforced (Req 11.3).

        For each due row, delivery is attempted via the injected
        :class:`MessageSender` (a raised exception is treated as a failure):

        * **Success (Req 11.5).** ``attempts`` is incremented, ``last_attempt_at``
          set to ``now``, the row marked ``DELIVERED`` and ``next_attempt_at``
          cleared. If the row had any prior failed attempt, that recorded failure
          is thereby resolved (recorded in the report's ``resolved`` bucket).

        * **Failure before the budget is spent (Req 11.3).** The failure is
          recorded (order id + recipient live on the row; the sender ``detail``
          and a monotonic failure timestamp are stored in the payload), ``attempts``
          is incremented, the row marked ``FAILED`` and ``next_attempt_at`` set to
          ``now + 30s`` so the next attempt is at least 30s later.

        * **Failure that exhausts the budget (Req 11.4).** After the 4th total
          attempt (1 + 3 retries) still fails, the row is marked ``UNDELIVERED``
          and ``next_attempt_at`` cleared. The order's ``Order_State`` is **never
          touched** by this service, so an undelivered notification leaves the
          order exactly where it was.

        Returns a :class:`DeliveryReport` summarising the tick. The work is done
        in the caller's open ``UnitOfWork``; the caller commits.
        """
        if self._sender is None:
            raise RuntimeError(
                "deliver_pending requires a MessageSender; construct "
                "NotificationService(uow, sender=...)"
            )

        report = DeliveryReport()
        due = self._uow.notifications.list_due(now)
        for notification in due:
            self._attempt_delivery(notification, now, report)
        return report

    # ------------------------------------------------------------------- #
    # internals
    # ------------------------------------------------------------------- #
    def _attempt_delivery(
        self, notification: Notification, now: datetime, report: DeliveryReport
    ) -> None:
        """Attempt one delivery and persist the resulting state (Req 11.3-11.5)."""
        had_prior_failure = notification.attempts > 0

        try:
            result: SendResult = as_send_result(
                self._sender.send(notification.recipient_id, notification.payload)
            )
        except Exception as exc:  # defensive: a raising sender == a failure
            result = SendResult.fail(f"sender raised: {type(exc).__name__}")

        # Every attempt counts toward the bounded budget and stamps the time.
        notification.attempts += 1
        notification.last_attempt_at = now

        if result.success:
            notification.status = NotificationStatus.DELIVERED
            notification.next_attempt_at = None
            if had_prior_failure:
                # Req 11.5: a late success resolves the previously recorded
                # failure. The row's transition out of FAILED *is* the
                # resolution; flag it on the payload so the resolution is
                # observable to later readers/renderers.
                self._record_failure_resolved(notification, now)
                report.resolved.append(notification.notification_id)
            self._uow.notifications.update(notification)
            report.delivered.append(notification.notification_id)
            return

        # Failure path. Record the failure with the order id + recipient (Req
        # 11.3); these already live on the row, so we additionally stamp the
        # sender detail + attempt time into the payload's failure record.
        self._record_failure(notification, now, result.detail)

        if notification.attempts >= MAX_DELIVERY_ATTEMPTS:
            # Req 11.4: budget exhausted -> undelivered, order state untouched.
            notification.status = NotificationStatus.UNDELIVERED
            notification.next_attempt_at = None
            self._uow.notifications.update(notification)
            report.undelivered.append(notification.notification_id)
            return

        # Req 11.3: schedule the next attempt >= 30s later.
        notification.status = NotificationStatus.FAILED
        notification.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS)
        self._uow.notifications.update(notification)
        report.failed.append(notification.notification_id)

    @staticmethod
    def _record_failure(notification: Notification, now: datetime, detail: str) -> None:
        """Record a delivery failure on the notification's payload (Req 11.3).

        The order id and intended recipient are intrinsic columns of the row;
        the failure log (attempt timestamps + sender detail) is appended to the
        ``payload`` under a ``delivery`` sub-document so the failure record
        travels with the versioned notification document and remains readable by
        any channel. Keeping it additive preserves forward-compatibility.
        """
        delivery = dict(notification.payload.get("delivery") or {})
        failures = list(delivery.get("failures") or [])
        failures.append(
            {
                "at": now.isoformat(),
                "recipient_id": str(notification.recipient_id),
                "order_id": str(notification.order_id),
                "detail": detail or "",
            }
        )
        delivery["failures"] = failures
        delivery["resolved"] = False
        payload = dict(notification.payload)
        payload["delivery"] = delivery
        notification.payload = payload

    @staticmethod
    def _record_failure_resolved(notification: Notification, now: datetime) -> None:
        """Mark a previously-recorded failure resolved on success (Req 11.5)."""
        delivery = dict(notification.payload.get("delivery") or {})
        delivery["resolved"] = True
        delivery["resolved_at"] = now.isoformat()
        payload = dict(notification.payload)
        payload["delivery"] = delivery
        notification.payload = payload

    # ------------------------------------------------------------------- #
    # design-named helpers (mark_resolved / mark_undelivered)
    # ------------------------------------------------------------------- #
    def mark_undelivered(self, notification_id) -> Optional[Notification]:
        """Force a notification to the terminal ``UNDELIVERED`` state (Req 11.4).

        Exposed per the design's Notification_Service API; the delivery loop sets
        this automatically when the retry budget is spent, but the Bot_Interface
        / an operator may also call it. Leaves the order's state untouched.
        """
        notification = self._uow.notifications.get(notification_id)
        if notification is None:
            return None
        notification.status = NotificationStatus.UNDELIVERED
        notification.next_attempt_at = None
        return self._uow.notifications.update(notification)

    def mark_resolved(self, notification_id) -> Optional[Notification]:
        """Mark a delivered notification's prior failure resolved (Req 11.5)."""
        notification = self._uow.notifications.get(notification_id)
        if notification is None:
            return None
        self._record_failure_resolved(notification, _utcnow())
        return self._uow.notifications.update(notification)


def _utcnow() -> datetime:
    """Timezone-aware current time (kept local so callers can stay zone-safe)."""
    from datetime import timezone

    return datetime.now(timezone.utc)
