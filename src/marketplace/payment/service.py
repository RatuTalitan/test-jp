"""Payment_Service: UPI presentation, UTR submission, screenshot attachment.

This is the channel- and DB-agnostic Payment_Service described in design.md ->
``Payment_Service``. Like the other domain services it reaches persistence
**only** through the repository protocols exposed by a
:class:`~marketplace.domain.repositories.UnitOfWork` and never imports the ORM
or opens a connection itself (design.md -> Architectural Principles). The caller
(the Bot_Interface) owns the transaction boundary; this service performs its
work inside that transaction and never commits on its own.

Every order-state change is routed through the single guarded
:func:`marketplace.order.state_machine.transition` function (task 10.1) -- this
service never assigns ``order.state`` directly.

Following the "stable status codes, never localized prose" rule, business-rule
refusals are returned as typed results (:class:`Rejected` / :class:`Conflict` /
:class:`NotFound`) carrying a **stable code** plus structured ``details`` that
the Bot_Interface maps to a localized Message_Catalog template.

Public API (tasks 11.1 / 11.2)::

    svc = PaymentService(uow, object_store=...)
    svc.present_instructions(order_id) -> UpiInstructions | Rejected | NotFound   # 11.1
    svc.submit_utr(order_id, utr, actor) -> Order | Rejected | Conflict | NotFound # 11.1
    svc.attach_screenshot(order_id, data, content_type, size)
        -> Payment | Rejected | NotFound                                          # 11.2

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 19.7.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Union

from marketplace.domain.entities import (
    DEFAULT_UTR_PATTERN,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderState,
    Payment,
    Role,
    User,
    new_id,
)
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import Conflict, NotFound, Rejected
from marketplace.order.state_machine import Actor, OrderEvent, transition
from marketplace.payment.object_store import ObjectStore

__all__ = [
    "PaymentService",
    "UpiInstructions",
    # stable status codes
    "UPI_NOT_CONFIGURED",
    "PAYMENT_NOT_PENDING",
    "INVALID_UTR_FORMAT",
    "DUPLICATE_UTR",
    "UNSUPPORTED_IMAGE_FORMAT",
    "IMAGE_TOO_LARGE",
    # screenshot policy
    "SUPPORTED_IMAGE_CONTENT_TYPES",
    "MAX_SCREENSHOT_BYTES",
    # notification sequencing
    "PAYMENT_SUBMITTED_TRANSITION_SEQ",
    # result aliases
    "SubmitUtrResult",
    "InstructionsResult",
    "AttachScreenshotResult",
]


# --- Stable status codes (language-agnostic; localized by the Bot_Interface) --
#: ``present_instructions`` was asked for an order but the Seller has not yet
#: configured a UPI address in ``seller_settings`` (Req 6.1/19.7). Nothing to
#: present; the response states that payment details are not configured.
UPI_NOT_CONFIGURED = "UPI_NOT_CONFIGURED"
#: A UTR was submitted while the order is **not** in PAYMENT_PENDING (Req 6.7).
#: The state is left unchanged and the response says the order is not awaiting a
#: payment reference.
PAYMENT_NOT_PENDING = "PAYMENT_NOT_PENDING"
#: The submitted UTR does not match the configured ``utr_pattern`` (Req 6.2/6.6).
#: ``details`` carries the required ``pattern`` so the message can state the
#: expected format. State unchanged.
INVALID_UTR_FORMAT = "INVALID_UTR_FORMAT"
#: The submitted UTR is already recorded against a **different** order (Req 6.4).
#: State unchanged; the response says the reference is already in use.
DUPLICATE_UTR = "DUPLICATE_UTR"
#: A screenshot was attached in an unsupported image format (Req 6.8). ``details``
#: lists the accepted formats and size cap.
UNSUPPORTED_IMAGE_FORMAT = "UNSUPPORTED_IMAGE_FORMAT"
#: A screenshot exceeding the size cap was attached (Req 6.8). ``details`` lists
#: the accepted formats and size cap.
IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"


#: Supported screenshot image formats (Req 6.3/6.8). The accepted set is a small
#: list of common, browser-renderable raster formats; an attachment whose
#: content type is outside this set is rejected with the accepted formats stated.
SUPPORTED_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)

#: Maximum accepted screenshot size: 10 MB (Req 6.3/6.8), expressed in bytes
#: using the binary mebibyte (10 * 1024 * 1024) so the boundary is exact.
MAX_SCREENSHOT_BYTES: int = 10 * 1024 * 1024

#: The ``transition_seq`` used for the "payment proof ready for verification"
#: seller notification's idempotency key. Placement is sequence 1 (PLACED ->
#: PAYMENT_PENDING); the UTR submission is the next transition (PAYMENT_PENDING
#: -> PAYMENT_SUBMITTED), sequence 2. Combined with ``(order_id, STATE_CHANGE)``
#: it makes the enqueue idempotent so a retried submission never doubles the
#: seller's verification message (Req 6.5; design.md -> Notifications).
PAYMENT_SUBMITTED_TRANSITION_SEQ = 2


@dataclass(frozen=True)
class UpiInstructions:
    """The Seller-configured payment details presented to the Customer (Req 6.1).

    ``upi_address`` is the current ``seller_settings.upi_address`` (the payee
    VPA); ``upi_qr_object_key`` references the **static** stored QR image
    (``seller_settings.upi_qr_object_key``) -- only the object-storage reference
    is carried, never the image bytes; ``amount_due`` is the order's total
    amount (Req 6.1/19.7). ``upi_qr_object_key`` may be ``None`` if the Seller
    has configured an address but not yet uploaded a QR image.
    """

    order_id: uuid.UUID
    upi_address: str
    amount_due: Decimal
    upi_qr_object_key: Optional[str] = None


#: A presentation request returns the instructions or a typed failure.
InstructionsResult = Union[UpiInstructions, Rejected, NotFound]
#: A UTR submission returns the transitioned order or a typed failure.
SubmitUtrResult = Union[Order, Rejected, Conflict, NotFound]
#: A screenshot attachment returns the updated payment row or a typed failure.
AttachScreenshotResult = Union[Payment, Rejected, NotFound]


class PaymentService:
    """Payment operations bound to a single :class:`UnitOfWork` (one per request).

    Constructed with the active ``UnitOfWork`` and (for screenshot attachment)
    an :class:`~marketplace.payment.object_store.ObjectStore`. Uses the
    ``orders``, ``payments``, ``seller_settings``, ``users`` and
    ``notifications`` repositories. Mutating operations leave the transaction
    open for the caller to commit; a rejected operation makes no persistent
    change.
    """

    def __init__(self, uow: UnitOfWork, object_store: Optional[ObjectStore] = None) -> None:
        self._uow = uow
        self._object_store = object_store

    # ----------------------------------------------------- present_instructions
    def present_instructions(self, order_id) -> InstructionsResult:
        """Return the Seller-configured UPI details + amount due (Req 6.1, 19.7).

        Reads the **current** ``seller_settings.upi_address`` and the static QR
        reference ``seller_settings.upi_qr_object_key`` (not env config) together
        with the order's total amount due. Returns:

        * :class:`~marketplace.domain.results.NotFound` if the order does not
          exist;
        * :class:`~marketplace.domain.results.Rejected` (``UPI_NOT_CONFIGURED``)
          if the Seller has not configured a UPI address yet (nothing to
          present);
        * otherwise a :class:`UpiInstructions` carrying the address, the QR
          object key, and ``amount_due = order.total_amount``.
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        settings = self._uow.seller_settings.get()
        if settings is None or not settings.upi_address:
            return Rejected(
                UPI_NOT_CONFIGURED,
                reason="the Seller has not configured a UPI address yet",
                details={"order_id": order_id},
            )

        return UpiInstructions(
            order_id=order.order_id,
            upi_address=settings.upi_address,
            amount_due=order.total_amount,
            upi_qr_object_key=settings.upi_qr_object_key,
        )

    # ---------------------------------------------------------------- submit_utr
    def submit_utr(self, order_id, utr: str, actor: Actor) -> SubmitUtrResult:
        """Record a UTR and transition PAYMENT_PENDING -> PAYMENT_SUBMITTED.

        Validation order (each leaves all data and the order state unchanged on
        failure):

        1. **Order exists** -- otherwise :class:`NotFound`.
        2. **State (Req 6.7)** -- the order must be in PAYMENT_PENDING; otherwise
           :class:`Rejected` (``PAYMENT_NOT_PENDING``).
        3. **Format (Req 6.2/6.6)** -- the UTR must match the **configured**
           ``seller_settings.utr_pattern`` (default exactly 12 alphanumeric);
           otherwise :class:`Rejected` (``INVALID_UTR_FORMAT``) stating the
           required pattern.
        4. **Global uniqueness (Req 6.4)** -- if the UTR is already recorded
           against a **different** order, :class:`Conflict` (``DUPLICATE_UTR``).

        On success (Req 6.2/6.5): the UTR (+ ``utr_submitted_at``) is recorded on
        the order's Payment row (created if absent), the order is transitioned
        through the guarded state machine (``SUBMIT_UTR``), and a Seller "payment
        proof ready for verification" notification is enqueued idempotently.
        """
        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        # 1. State gate (Req 6.7) -- before touching format/uniqueness so a UTR
        # submitted out of state never mutates anything.
        if order.state is not OrderState.PAYMENT_PENDING:
            return Rejected(
                PAYMENT_NOT_PENDING,
                reason="the order is not awaiting a payment reference",
                details={"order_id": order_id, "state": order.state.value},
            )

        # 2. Format against the CONFIGURED pattern (Req 6.2/6.6).
        pattern = self._configured_utr_pattern()
        if utr is None or re.match(pattern, utr) is None:
            return Rejected(
                INVALID_UTR_FORMAT,
                reason="the submitted UTR does not match the required format",
                details={"pattern": pattern},
            )

        # 3. Global uniqueness (Req 6.4): a non-null UTR may exist against at most
        # one order. A match on the *same* order is benign; a different order is a
        # conflict.
        existing = self._uow.payments.get_by_utr(utr)
        if existing is not None and existing.order_id != order_id:
            return Conflict(
                DUPLICATE_UTR,
                reason="this payment reference is already in use",
                details={"utr": utr, "order_id": order_id},
            )

        # 4. Apply the guarded transition on the in-memory order first; if it is
        # refused (e.g. wrong actor role) nothing has been written yet.
        moved = transition(order, OrderEvent.SUBMIT_UTR, actor)
        if not isinstance(moved, Order):
            return moved
        order = moved

        # 5. Record the UTR on the order's Payment row (create if absent), persist
        # the transitioned order, and enqueue the seller notification -- all within
        # the caller's open transaction.
        self._record_utr(order_id, utr)
        stored = self._uow.orders.update(order)
        self._enqueue_payment_submitted_notification(stored)
        return stored

    # --------------------------------------------------------- attach_screenshot
    def attach_screenshot(
        self,
        order_id,
        data: bytes,
        content_type: str,
        size: int,
    ) -> AttachScreenshotResult:
        """Attach an optional payment screenshot to the order (Req 6.3/6.8).

        Validates a supported image format and a size within the 10 MB cap; on
        success stores the blob in object storage and records **only** the
        returned ``screenshot_object_key`` on the order's Payment row (the blob
        is never persisted in the DB). Returns:

        * :class:`NotFound` if the order does not exist;
        * :class:`Rejected` (``UNSUPPORTED_IMAGE_FORMAT`` / ``IMAGE_TOO_LARGE``)
          stating the accepted format + size on a rejected attachment (Req 6.8);
        * otherwise the updated :class:`~marketplace.domain.entities.Payment`.

        Args:
            order_id: the target order.
            data: the screenshot bytes (or a provider file reference's bytes).
            content_type: the declared image MIME type (e.g. ``image/jpeg``).
            size: the attachment size in bytes (validated against the cap).
        """
        if self._object_store is None:
            raise ValueError(
                "attach_screenshot requires an ObjectStore; construct "
                "PaymentService(uow, object_store=...)"
            )

        order = self._uow.orders.get(order_id)
        if order is None:
            return NotFound("order", order_id)

        accepted = {
            "accepted_formats": sorted(SUPPORTED_IMAGE_CONTENT_TYPES),
            "max_bytes": MAX_SCREENSHOT_BYTES,
        }

        # Format check (Req 6.8).
        if content_type not in SUPPORTED_IMAGE_CONTENT_TYPES:
            return Rejected(
                UNSUPPORTED_IMAGE_FORMAT,
                reason="the attachment is not a supported image format",
                details={"content_type": content_type, **accepted},
            )

        # Size check (Req 6.3/6.8): the boundary itself (exactly 10 MB) is allowed.
        if size is None or size < 0 or size > MAX_SCREENSHOT_BYTES:
            return Rejected(
                IMAGE_TOO_LARGE,
                reason="the attachment exceeds the maximum allowed size",
                details={"size": size, **accepted},
            )

        # Store the blob in object storage; only the returned key reaches the DB.
        key = self._screenshot_key(order_id, content_type)
        stored_key = self._object_store.put(key, data, content_type)

        payment = self._get_or_create_payment(order_id)
        payment.screenshot_object_key = stored_key
        payment.updated_at = datetime.now(timezone.utc)
        return self._uow.payments.update(payment)

    # ------------------------------------------------------------------ internals
    def _configured_utr_pattern(self) -> str:
        """Return the Seller-configured UTR pattern, or the default if unset.

        Reads ``seller_settings.utr_pattern`` (Req 6.2/6.6). When no settings row
        has been seeded yet the default exactly-12-alphanumeric pattern applies.
        """
        settings = self._uow.seller_settings.get()
        if settings is not None and settings.utr_pattern:
            return settings.utr_pattern
        return DEFAULT_UTR_PATTERN

    def _get_or_create_payment(self, order_id) -> Payment:
        """Return the order's Payment row, creating an empty one if absent.

        Order placement (task 10.2) does not create a Payment row, so the first
        UTR submission or screenshot attachment materializes it.
        """
        payment = self._uow.payments.get_by_order(order_id)
        if payment is not None:
            return payment
        created = Payment(
            payment_id=new_id(),
            order_id=order_id,
            created_at=datetime.now(timezone.utc),
        )
        return self._uow.payments.add(created)

    def _record_utr(self, order_id, utr: str) -> Payment:
        """Record ``utr`` + submission timestamp on the order's Payment row."""
        payment = self._get_or_create_payment(order_id)
        payment.utr = utr
        payment.utr_submitted_at = datetime.now(timezone.utc)
        payment.updated_at = payment.utr_submitted_at
        return self._uow.payments.update(payment)

    @staticmethod
    def _screenshot_key(order_id, content_type: str) -> str:
        """Build a unique private object-storage key for a screenshot blob."""
        ext = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }.get(content_type, "bin")
        return f"payment-screenshots/{order_id}/{new_id()}.{ext}"

    def _find_seller(self) -> Optional[User]:
        """Return the configured Seller (the single ADMIN user), or ``None``."""
        for candidate in self._uow.users.list_all():
            if candidate.role == Role.ADMIN:
                return candidate
        return None

    def _enqueue_payment_submitted_notification(
        self, order: Order
    ) -> Optional[Notification]:
        """Enqueue the Seller "payment proof ready for verification" message.

        Writes a PENDING ``notifications`` row in the **same transaction** as the
        UTR submission, keyed idempotently by ``(order_id, STATE_CHANGE,
        PAYMENT_SUBMITTED_TRANSITION_SEQ)`` so a retried submission never enqueues
        a duplicate (Req 6.5; design.md -> Notifications: idempotency). If no
        Seller is configured there is no recipient, so the enqueue is skipped
        (the submission still succeeds).
        """
        seller = self._find_seller()
        if seller is None:
            return None

        existing = self._uow.notifications.find_by_idempotency_key(
            order.order_id,
            NotificationKind.STATE_CHANGE,
            PAYMENT_SUBMITTED_TRANSITION_SEQ,
        )
        if existing is not None:
            return existing

        notification = Notification(
            notification_id=new_id(),
            order_id=order.order_id,
            recipient_id=seller.user_id,
            kind=NotificationKind.STATE_CHANGE,
            transition_seq=PAYMENT_SUBMITTED_TRANSITION_SEQ,
            payload={
                "format_version": 1,
                "kind": NotificationKind.STATE_CHANGE.value,
                "event": "payment_proof_ready",
                "order_id": str(order.order_id),
                "new_state": order.state.value,
            },
            status=NotificationStatus.PENDING,
        )
        return self._uow.notifications.add(notification)
