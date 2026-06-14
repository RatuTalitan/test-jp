"""Typed result objects shared across the domain services (task 4.1).

Domain services return a successful entity **or** one of these channel-agnostic
*failure* result objects, never localized prose (design.md -> Bot_Interface ->
"Domain services hand back typed results and stable status/error codes"). The
Bot_Interface maps the ``code`` carried here to a localized Message_Catalog
template for the user's active language (Req 18); the failure objects therefore
hold a **stable status code** plus optional structured data, not user-facing
copy.

These objects are intentionally immutable (``frozen=True``) and hashable so they
are cheap to compare in tests and safe to pass around. A ``Failure`` type alias
groups them for use in service return-type annotations, e.g.::

    def require_admin(uid) -> "ok | NotAuthorized": ...

Task 4.1 defines the shared vocabulary only; individual services attach their
own concrete ``code`` strings (e.g. ``QTY_BELOW_MOQ``, ``DUPLICATE_UTR``,
``NOT_AUTHORIZED``) in later tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

__all__ = [
    "Rejected",
    "NotFound",
    "NotAuthorized",
    "Unauthenticated",
    "Conflict",
    "Failure",
    "is_failure",
]


@dataclass(frozen=True)
class Rejected:
    """A request understood but refused by a business rule.

    ``code`` is the stable status code the presentation layer localizes (e.g.
    ``QTY_BELOW_MOQ``, ``STOCK_EXCEEDED``, ``INVALID_UTR_FORMAT``). ``reason`` is
    a short developer-facing explanation (never shown verbatim to users).
    ``details`` carries structured placeholders (e.g. the offending ``moq`` or
    ``stock`` value) for the localized template to interpolate.
    """

    code: str
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NotFound:
    """The referenced entity does not exist.

    ``entity`` names the aggregate (e.g. ``"product"``, ``"order"``); ``identifier``
    is the lookup key that missed (kept generic so it works for UUIDs, ints, or
    natural keys).
    """

    entity: str
    identifier: Optional[Any] = None
    code: str = "NOT_FOUND"


@dataclass(frozen=True)
class NotAuthorized:
    """The actor is authenticated but lacks permission (e.g. non-Seller calling
    an Admin_Console entry point; Req 1.7/12.1). No data is changed."""

    reason: str = "Seller privileges required"
    code: str = "NOT_AUTHORIZED"


@dataclass(frozen=True)
class Unauthenticated:
    """No verified identity for the actor (no Verified_Contact, Req 12.6)."""

    reason: str = "Authentication required"
    code: str = "UNAUTHENTICATED"


@dataclass(frozen=True)
class Conflict:
    """The request conflicts with current state (e.g. a duplicate UTR, Req 6.4,
    or a stock conflict at approval, Req 7.4).

    ``details`` carries the conflicting data (e.g. affected lines + available
    stock) for the localized message.
    """

    code: str
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


# Union of every failure result a service may return. Services annotate methods
# as ``Entity | <subset of Failure>``; this alias is the full vocabulary.
Failure = Union[Rejected, NotFound, NotAuthorized, Unauthenticated, Conflict]

_FAILURE_TYPES = (Rejected, NotFound, NotAuthorized, Unauthenticated, Conflict)


def is_failure(value: object) -> bool:
    """Return True if ``value`` is any of the typed failure results.

    A small convenience so callers/tests can branch on success-vs-failure
    without importing every individual type.
    """
    return isinstance(value, _FAILURE_TYPES)
