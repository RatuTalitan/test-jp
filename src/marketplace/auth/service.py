"""Auth_Service implementation (task 6.1).

The Auth_Service establishes and stores a user's ``Verified_Contact`` (from the
Telegram *Share Contact* action), identifies returning users, assigns roles
(``ADMIN`` only for the configured Seller), manages the
``Offline_Payment_Allowed`` flag, and persists the per-user
``Language_Preference`` (design.md -> Components -> Auth_Service; Req 1, 12.1,
17.1/17.3, 18.4/18.5/18.8).

Design alignment / boundaries:
  * **Persistence only through the repository protocols / UnitOfWork.** The
    service never imports the ORM or opens a session; it receives a
    ``uow_factory`` (a zero-arg callable returning a fresh
    :class:`~marketplace.domain.repositories.UnitOfWork`) and performs each
    operation inside one ``with uow: ... uow.commit()`` block, modelling the
    design's "one atomic transaction per operation" boundary (design.md ->
    Transaction Boundaries; Req 1.9).
  * **Language-agnostic.** Methods return either a domain entity (e.g.
    :class:`~marketplace.domain.entities.User`) or a typed result object
    (:class:`Ok`, ``Rejected``/``NotFound``/``NotAuthorized``/``Unauthenticated``)
    carrying a **stable status code** — never localized prose. The Bot_Interface
    maps those codes to localized Message_Catalog templates (Req 18).
  * **Seller identity is configuration, not data.** ``role_of`` returns ``ADMIN``
    only for the ``SELLER_TELEGRAM_ID`` supplied at construction (design.md ->
    Security Design -> Admin (Seller) Gating; Req 1.6).

This module defines the clean, importable API (``AuthService``,
``SharedContact``, ``Ok``/``OK``) the Admin_Console and Bot_Interface call later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Union

from marketplace.domain.entities import Language, Role, User, new_id
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import (
    NotAuthorized,
    NotFound,
    Rejected,
    Unauthenticated,
)

__all__ = [
    "SharedContact",
    "Ok",
    "OK",
    "AuthService",
    "effective_language",
    # Stable status codes (presentation layer localizes these).
    "CODE_CONTACT_IDENTITY_MISMATCH",
    "CODE_IDENTIFICATION_FAILED",
    "CODE_INVALID_LANGUAGE",
]

# --- Stable status codes ----------------------------------------------------
# These are the language-independent codes the Bot_Interface maps to localized
# copy via the Message_Catalog (Req 18). They are NOT user-facing strings.
CODE_CONTACT_IDENTITY_MISMATCH = "CONTACT_IDENTITY_MISMATCH"
CODE_IDENTIFICATION_FAILED = "IDENTIFICATION_FAILED"
CODE_INVALID_LANGUAGE = "INVALID_LANGUAGE"


@dataclass(frozen=True)
class SharedContact:
    """The payload of a Telegram *Share Contact* action.

    ``user_id`` is the **Telegram user id carried by the shared contact** and is
    present only when the contact is itself a Telegram user; it is ``None`` for
    a contact shared from the phone book that is not a Telegram account. The
    identity check (Req 1.2/1.8) compares this against the sender's Telegram user
    id. ``phone_number`` is the value stored as the ``Verified_Contact`` (the
    persistence layer is responsible for encrypting it at rest, design.md ->
    Security Design -> PII Handling).
    """

    phone_number: str
    user_id: Optional[int] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


@dataclass(frozen=True)
class Ok:
    """A successful, data-less authorization result (the design's ``ok``).

    Returned by :meth:`AuthService.require_admin` when the actor is the Seller.
    Carries a stable ``code`` for symmetry with the failure results.
    """

    code: str = "OK"


#: Shared singleton ``Ok`` instance (the result is immutable and stateless).
OK = Ok()

# A reference to a customer accepted by ``set_offline_payment_allowed`` /
# ``set_language_preference``: the internal surrogate ``user_id`` (UUID), the
# external Telegram user id (int), or a stored ``Verified_Contact`` (str).
CustomerRef = Union[uuid.UUID, int, str]


def _utcnow() -> datetime:
    """Timezone-aware current UTC timestamp (matches ``TIMESTAMPTZ`` columns)."""
    return datetime.now(timezone.utc)


def effective_language(language_preference: Optional[Language]) -> Language:
    """Resolve a stored preference to an effective language.

    An unset/``None`` preference resolves to **Hindi** (Req 18.2/18.4). Provided
    as the single source of the "NULL means Hindi" rule for the renderer; the
    Auth_Service itself stores ``None`` faithfully and never invents a value.
    """
    return language_preference if language_preference is not None else Language.HI


class AuthService:
    """Identity, roles, offline-payment flag, and language preference (Req 1,
    12.1, 17, 18).

    Args:
        uow_factory: a zero-argument callable returning a fresh
            :class:`~marketplace.domain.repositories.UnitOfWork`. Each public
            method opens exactly one transaction from it.
        seller_telegram_id: the configured ``SELLER_TELEGRAM_ID``; the **only**
            Telegram id that maps to the ``ADMIN`` role (Req 1.6).
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        seller_telegram_id: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._seller_telegram_id = int(seller_telegram_id)

    # -- Roles --------------------------------------------------------------
    def role_of(self, telegram_user_id: int) -> Role:
        """Return ``ADMIN`` for the configured Seller, else ``CUSTOMER`` (Req 1.6).

        Pure/config-driven: the Seller is identified by configuration, not by a
        stored row, so no transaction is needed.
        """
        if telegram_user_id == self._seller_telegram_id:
            return Role.ADMIN
        return Role.CUSTOMER

    def require_admin(self, telegram_user_id: int) -> Union[Ok, NotAuthorized]:
        """Gate Seller-only actions (Req 1.7/12.1).

        Returns :data:`OK` for the Seller; otherwise :class:`NotAuthorized` with
        no data change. Every Admin_Console entry point calls this first.
        """
        if self.role_of(telegram_user_id) is Role.ADMIN:
            return OK
        return NotAuthorized()

    # -- Identity -----------------------------------------------------------
    def register_contact(
        self, telegram_user_id: int, shared_contact: SharedContact
    ) -> Union[User, Rejected]:
        """Store a ``Verified_Contact`` iff the shared identity matches (Req 1.2,
        1.8, 1.9).

        The contact is stored **only** when ``shared_contact.user_id`` equals the
        sender's ``telegram_user_id``; on a mismatch nothing is stored and a
        typed :class:`Rejected` (code :data:`CODE_CONTACT_IDENTITY_MISMATCH`) is
        returned (Req 1.8). The write is performed in a single transaction that
        either commits fully or rolls back, so a storage failure leaves **no
        partial identity** and yields :class:`Rejected`
        (:data:`CODE_IDENTIFICATION_FAILED`, Req 1.9). On success the user's
        ``contact_verified_at`` is set and the stored :class:`User` is returned.
        """
        # Identity check (Req 1.2/1.8): a missing or non-matching contact id is a
        # rejection that stores nothing.
        if (
            shared_contact.user_id is None
            or shared_contact.user_id != telegram_user_id
        ):
            return Rejected(
                code=CODE_CONTACT_IDENTITY_MISMATCH,
                reason="shared contact id does not match the sender",
                details={"telegram_user_id": telegram_user_id},
            )

        try:
            with self._uow_factory() as uow:
                now = _utcnow()
                existing = uow.users.get_by_telegram_id(telegram_user_id)
                if existing is None:
                    user = User(
                        user_id=new_id(),
                        telegram_user_id=telegram_user_id,
                        role=self.role_of(telegram_user_id),
                        verified_contact=shared_contact.phone_number,
                        contact_verified_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    uow.users.add(user)
                else:
                    existing.verified_contact = shared_contact.phone_number
                    existing.contact_verified_at = now
                    # Role is derived from configuration; keep it authoritative.
                    existing.role = self.role_of(telegram_user_id)
                    existing.updated_at = now
                    uow.users.update(existing)
                # Commit atomically; only after this does the identity persist.
                uow.commit()
                stored = uow.users.get_by_telegram_id(telegram_user_id)
                # Defensive: a backend that silently dropped the write leaves the
                # user unauthenticated rather than reporting false success.
                if stored is None or stored.contact_verified_at is None:
                    return Rejected(
                        code=CODE_IDENTIFICATION_FAILED,
                        reason="verified contact was not persisted",
                        details={"telegram_user_id": telegram_user_id},
                    )
                return stored
        except Exception:
            # Any storage error: the transaction rolled back (no partial
            # identity), and we report that identification could not complete.
            return Rejected(
                code=CODE_IDENTIFICATION_FAILED,
                reason="storage failure while persisting the verified contact",
                details={"telegram_user_id": telegram_user_id},
            )

    def identify(
        self, telegram_user_id: int
    ) -> Union[User, Unauthenticated]:
        """Identify a returning user without re-requesting contact (Req 1.3).

        A *returning user* is one whose Telegram id already has a stored
        ``Verified_Contact``; such a user is returned directly. Anyone else
        (unknown, or known but without a verified contact) yields
        :class:`Unauthenticated`.
        """
        with self._uow_factory() as uow:
            user = uow.users.get_by_telegram_id(telegram_user_id)
            # Read-only: no commit needed.
        if user is not None and user.is_authenticated:
            return user
        return Unauthenticated()

    # -- Offline-payment flag ----------------------------------------------
    def set_offline_payment_allowed(
        self,
        target_customer_ref: CustomerRef,
        enabled: bool,
        acting_user: Union[int, User],
    ) -> Union[User, NotFound, NotAuthorized]:
        """Enable/disable a Customer's ``Offline_Payment_Allowed`` flag (Req
        17.1/17.3, 12.1).

        Seller-only: a non-Seller ``acting_user`` is rejected with
        :class:`NotAuthorized` and no change (Req 17.2/12.1). An unknown
        ``target_customer_ref`` yields :class:`NotFound` with nothing persisted
        (Req 17.3). On success the updated :class:`User` is returned (Req 17.1).
        """
        acting_telegram_id = (
            acting_user.telegram_user_id
            if isinstance(acting_user, User)
            else acting_user
        )
        if self.role_of(acting_telegram_id) is not Role.ADMIN:
            return NotAuthorized()

        try:
            with self._uow_factory() as uow:
                target = self._resolve_user(uow, target_customer_ref)
                if target is None:
                    # Nothing persisted on an unknown customer (Req 17.3).
                    return NotFound(entity="customer", identifier=target_customer_ref)
                target.offline_payment_allowed = bool(enabled)
                target.updated_at = _utcnow()
                uow.users.update(target)
                uow.commit()
                return uow.users.get(target.user_id)
        except Exception:
            # A storage failure leaves the flag unchanged (transaction rolled
            # back); surface it as "not found" for the caller to retry rather
            # than reporting a false success.
            return NotFound(entity="customer", identifier=target_customer_ref)

    # -- Language preference -----------------------------------------------
    def set_language_preference(
        self, user_id: CustomerRef, language: Union[Language, str]
    ) -> Union[User, NotFound, Rejected]:
        """Persist a user's presentation ``Language_Preference`` (Req 18.4/18.5/18.8).

        Works identically for Customers and the Seller (role-independent, Req
        18.8). ``language`` accepts the :class:`Language` enum or the literals
        ``'HI'`` / ``'EN'``; an unrecognized value yields :class:`Rejected`
        (:data:`CODE_INVALID_LANGUAGE`) with no change. An unknown ``user_id``
        yields :class:`NotFound`. On success the updated :class:`User` (with the
        stored preference) is returned so the caller can re-render immediately.
        """
        normalized = self._normalize_language(language)
        if normalized is None:
            return Rejected(
                code=CODE_INVALID_LANGUAGE,
                reason="language must be one of 'HI' or 'EN'",
                details={"language": str(language)},
            )

        with self._uow_factory() as uow:
            target = self._resolve_user(uow, user_id)
            if target is None:
                return NotFound(entity="user", identifier=user_id)
            target.language_preference = normalized
            target.updated_at = _utcnow()
            uow.users.update(target)
            uow.commit()
            return uow.users.get(target.user_id)

    # -- Internals ----------------------------------------------------------
    @staticmethod
    def _normalize_language(language: Union[Language, str]) -> Optional[Language]:
        """Coerce a Language/str to a :class:`Language`, or ``None`` if invalid."""
        if isinstance(language, Language):
            return language
        if isinstance(language, str):
            try:
                return Language(language.strip().upper())
            except ValueError:
                return None
        return None

    @staticmethod
    def _resolve_user(uow: UnitOfWork, ref: CustomerRef) -> Optional[User]:
        """Resolve a customer reference to a stored :class:`User`, or ``None``.

        Accepts the internal surrogate ``user_id`` (UUID), the Telegram user id
        (int), or a stored ``Verified_Contact`` (str) — matching the "identified
        by Verified_Contact or Telegram user identifier" wording of Req 17.1.
        """
        if isinstance(ref, uuid.UUID):
            return uow.users.get(ref)
        if isinstance(ref, bool):  # guard: bool is a subclass of int
            return None
        if isinstance(ref, int):
            return uow.users.get_by_telegram_id(ref)
        if isinstance(ref, str):
            # Try a verified-contact match first, then a numeric Telegram id.
            for user in uow.users.list_all():
                if user.verified_contact == ref:
                    return user
            if ref.isdigit():
                return uow.users.get_by_telegram_id(int(ref))
            return None
        return None
