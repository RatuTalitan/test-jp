"""The ``MessageSender`` delivery port for the Notification_Service (task 18.2).

The retry worker (:meth:`marketplace.notification.service.NotificationService.deliver_pending`)
must actually *deliver* a queued notification to its recipient, but the domain
layer never imports the Telegram SDK (design.md -> Architectural Principles:
"Channel abstraction ... Domain services never import the Telegram SDK"). This
module defines the thin seam between the channel-agnostic delivery loop and the
concrete transport:

  * :class:`MessageSender` -- a :class:`typing.Protocol` with a single
    ``send(recipient_id, payload) -> SendResult`` method. The real adapter wired
    to Telegram via the Bot_Interface ``MessagingChannel`` is **task 19** (and
    the final wiring task 22); the Notification_Service depends only on this
    protocol so it stays testable with a fake sender and channel-swappable for
    WhatsApp / web in Phase 2.
  * :class:`SendResult` -- the channel-agnostic success/failure outcome of one
    delivery attempt, carrying an optional short ``detail`` recorded against the
    notification's failure record (Req 11.3) without leaking transport-specific
    objects into the domain.

The payload handed to :meth:`MessageSender.send` is the notification's stored
``{format_version, ...}`` JSON document; rendering it into localized prose for
the recipient's active language is the Bot_Interface's concern (Req 18), not the
sender port's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "SendResult",
    "MessageSender",
    "as_send_result",
]


@dataclass(frozen=True)
class SendResult:
    """The outcome of a single delivery attempt (channel-agnostic).

    ``success`` is the only field the delivery loop branches on; ``detail`` is a
    short, developer-facing note (e.g. a transport error summary) recorded
    against the notification's failure record (Req 11.3) and never shown to a
    user. Construct via :meth:`ok` / :meth:`fail` for readability.
    """

    success: bool
    detail: str = ""

    @classmethod
    def ok(cls, detail: str = "") -> "SendResult":
        """A successful delivery outcome."""
        return cls(True, detail)

    @classmethod
    def fail(cls, detail: str = "") -> "SendResult":
        """A failed delivery outcome (triggers the retry/backoff path)."""
        return cls(False, detail)


@runtime_checkable
class MessageSender(Protocol):
    """Port that delivers one notification payload to one recipient.

    Implementations adapt this to a concrete channel (the Telegram
    ``MessagingChannel`` in task 19); the Notification_Service depends only on
    this protocol. An implementation **should not raise** for an ordinary
    delivery failure -- it should return ``SendResult.fail(...)`` so the worker
    records the failure and schedules a retry; the worker also defensively
    treats a raised exception as a failure.
    """

    def send(self, recipient_id, payload: Mapping[str, Any]) -> "SendResult":
        """Attempt delivery; return success/failure (never localized prose)."""
        ...


def as_send_result(value: Any) -> SendResult:
    """Normalise a sender return value into a :class:`SendResult`.

    Accepts a :class:`SendResult` (returned as-is) or a plain ``bool`` (``True``
    -> success, ``False`` -> failure), so a minimal sender may simply return a
    boolean. Any other truthy/falsey value is coerced by its truthiness, keeping
    the port forgiving for trivial adapters and tests.
    """
    if isinstance(value, SendResult):
        return value
    if isinstance(value, bool):
        return SendResult.ok() if value else SendResult.fail()
    return SendResult.ok() if bool(value) else SendResult.fail()
