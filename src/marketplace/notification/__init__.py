"""Notification_Service subsystem (task 18).

Delivers state/event messages to Customers and the Seller with bounded retry,
idempotent ``(order_id, kind, transition_seq)`` rows, and failure recording.
Language-agnostic: payloads carry stable codes/data, rendered by the
Bot_Interface (task 19/22) into the recipient's active language (Req 18).

Public API (the seam the Bot_Interface wires to Telegram):

  * :class:`~marketplace.notification.service.NotificationService` -- the
    transactional, idempotent :meth:`enqueue` (task 18.1) plus the two payload
    helpers :meth:`enqueue_customer_state_change` (Req 11.1) and
    :meth:`enqueue_seller_new_order` (Req 11.2), and the retry-worker
    :meth:`deliver_pending` loop (task 18.2, Req 11.3-11.5).
  * :class:`~marketplace.notification.sender.MessageSender` -- the delivery port
    the real Telegram adapter (task 19) implements; the service depends only on
    this protocol, never on the Telegram SDK.
  * :class:`~marketplace.notification.sender.SendResult` -- the channel-agnostic
    per-attempt success/failure outcome.
"""

from marketplace.notification.sender import (
    MessageSender,
    SendResult,
    as_send_result,
)
from marketplace.notification.service import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    DeliveryReport,
    NotificationService,
)

__all__ = [
    # service (tasks 18.1 + 18.2)
    "NotificationService",
    "DeliveryReport",
    "MAX_DELIVERY_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    # delivery port (seam for Telegram, task 19)
    "MessageSender",
    "SendResult",
    "as_send_result",
]
