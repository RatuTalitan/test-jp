"""Focused unit/example tests for the Auth_Service (task 6.1).

These verify the behaviours called out for task 6.1 using the fast in-memory
UnitOfWork (no database):

  * contact stored only on matching identity, with ``contact_verified_at`` set
    (Req 1.2/1.8);
  * contact-mismatch rejection stores nothing (Req 1.8);
  * storage-failure atomicity leaves no partial identity (Req 1.9);
  * stable returning-user identification without re-requesting contact (Req 1.3);
  * role assignment + admin gating (Req 1.6/1.7/12.1);
  * offline-payment flag set + unknown-customer NotFound + non-Seller gating
    (Req 17.1/17.2/17.3);
  * language-preference persistence round-trip for Customer and Seller, and the
    Hindi default for an unset preference (Req 18.4/18.5/18.8/18.2).

The property-based tests for these behaviours are the separate (optional) tasks
6.2-6.4 and are intentionally NOT implemented here.
"""

import uuid

import pytest

from marketplace.auth import (
    CODE_CONTACT_IDENTITY_MISMATCH,
    CODE_IDENTIFICATION_FAILED,
    CODE_INVALID_LANGUAGE,
    OK,
    AuthService,
    SharedContact,
    effective_language,
)
from marketplace.domain import (
    InMemoryDatabase,
    InMemoryUnitOfWork,
    Language,
    NotAuthorized,
    NotFound,
    Rejected,
    Role,
    Unauthenticated,
    User,
)

SELLER_TELEGRAM_ID = 999_000_111
CUSTOMER_TELEGRAM_ID = 555_222_333


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def db() -> InMemoryDatabase:
    """A fresh shared in-memory store for each test."""
    return InMemoryDatabase()


@pytest.fixture
def service(db: InMemoryDatabase) -> AuthService:
    """An AuthService whose UoW factory shares the single in-memory store."""
    return AuthService(
        uow_factory=lambda: InMemoryUnitOfWork(db),
        seller_telegram_id=SELLER_TELEGRAM_ID,
    )


class _FailingUserRepo:
    """A user repo whose writes always raise (storage-failure injection)."""

    def __init__(self, real):
        self._real = real

    def add(self, user):  # noqa: D401 - fault injection
        raise RuntimeError("simulated storage failure")

    def update(self, user):
        raise RuntimeError("simulated storage failure")

    # Reads delegate to the real repo so the pre-write lookup works.
    def get(self, user_id):
        return self._real.get(user_id)

    def get_by_telegram_id(self, telegram_user_id):
        return self._real.get_by_telegram_id(telegram_user_id)

    def list_all(self):
        return self._real.list_all()


class _FailingWriteUnitOfWork(InMemoryUnitOfWork):
    """An in-memory UoW whose user writes fail, to exercise Req 1.9 atomicity."""

    def __init__(self, db: InMemoryDatabase) -> None:
        super().__init__(db)
        self.users = _FailingUserRepo(self.users)


# --------------------------------------------------------------------------- #
# register_contact (Req 1.2, 1.8, 1.9)
# --------------------------------------------------------------------------- #
def test_register_contact_stores_verified_contact_on_matching_identity(service, db):
    contact = SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID)

    result = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)

    assert isinstance(result, User)
    assert result.verified_contact == "+919812345678"
    assert result.contact_verified_at is not None  # success sets the timestamp
    assert result.is_authenticated is True
    assert result.role is Role.CUSTOMER
    # Persisted in the store.
    stored = next(iter(db.users.values()))
    assert stored.telegram_user_id == CUSTOMER_TELEGRAM_ID
    assert stored.verified_contact == "+919812345678"


def test_register_contact_rejects_mismatched_identity_and_stores_nothing(service, db):
    # Shared contact belongs to a different Telegram user than the sender.
    contact = SharedContact(phone_number="+910000000000", user_id=CUSTOMER_TELEGRAM_ID + 1)

    result = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)

    assert isinstance(result, Rejected)
    assert result.code == CODE_CONTACT_IDENTITY_MISMATCH
    assert db.users == {}  # nothing stored


def test_register_contact_rejects_when_shared_contact_has_no_telegram_id(service, db):
    # A phone-book contact that is not a Telegram user carries no user_id.
    contact = SharedContact(phone_number="+910000000000", user_id=None)

    result = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)

    assert isinstance(result, Rejected)
    assert result.code == CODE_CONTACT_IDENTITY_MISMATCH
    assert db.users == {}


def test_register_contact_storage_failure_leaves_no_partial_identity(db):
    # A UoW whose user writes fail must roll back: no partial identity persists.
    service = AuthService(
        uow_factory=lambda: _FailingWriteUnitOfWork(db),
        seller_telegram_id=SELLER_TELEGRAM_ID,
    )
    contact = SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID)

    result = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)

    assert isinstance(result, Rejected)
    assert result.code == CODE_IDENTIFICATION_FAILED
    assert db.users == {}  # all-or-nothing: nothing stored (Req 1.9)


def test_register_contact_updates_existing_user_contact(service):
    contact = SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID)
    first = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)
    # Re-share with a corrected number.
    updated_contact = SharedContact(phone_number="+919898989898", user_id=CUSTOMER_TELEGRAM_ID)
    second = service.register_contact(CUSTOMER_TELEGRAM_ID, updated_contact)

    assert isinstance(second, User)
    assert second.user_id == first.user_id  # same identity, updated in place
    assert second.verified_contact == "+919898989898"


# --------------------------------------------------------------------------- #
# identify (Req 1.3)
# --------------------------------------------------------------------------- #
def test_identify_returns_stable_user_for_returning_user(service):
    contact = SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID)
    registered = service.register_contact(CUSTOMER_TELEGRAM_ID, contact)

    first = service.identify(CUSTOMER_TELEGRAM_ID)
    second = service.identify(CUSTOMER_TELEGRAM_ID)

    assert isinstance(first, User)
    assert first.user_id == registered.user_id
    # Stable across calls (Req 1.3) - same surrogate id, no re-request.
    assert second.user_id == first.user_id


def test_identify_unknown_user_is_unauthenticated(service):
    assert isinstance(service.identify(424242), Unauthenticated)


def test_identify_user_without_verified_contact_is_unauthenticated(service, db):
    # A user row that never completed contact verification is not "returning".
    user = User(
        user_id=uuid.uuid4(),
        telegram_user_id=CUSTOMER_TELEGRAM_ID,
        role=Role.CUSTOMER,
    )
    db.users[user.user_id] = user

    assert isinstance(service.identify(CUSTOMER_TELEGRAM_ID), Unauthenticated)


# --------------------------------------------------------------------------- #
# role_of + require_admin (Req 1.6, 1.7, 12.1)
# --------------------------------------------------------------------------- #
def test_role_of_admin_only_for_seller(service):
    assert service.role_of(SELLER_TELEGRAM_ID) is Role.ADMIN
    assert service.role_of(CUSTOMER_TELEGRAM_ID) is Role.CUSTOMER


def test_require_admin_allows_seller_and_blocks_others(service):
    assert service.require_admin(SELLER_TELEGRAM_ID) is OK
    assert isinstance(service.require_admin(CUSTOMER_TELEGRAM_ID), NotAuthorized)


# --------------------------------------------------------------------------- #
# set_offline_payment_allowed (Req 17.1, 17.2, 17.3, 12.1)
# --------------------------------------------------------------------------- #
def test_set_offline_payment_allowed_by_seller_sets_flag(service):
    service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )

    result = service.set_offline_payment_allowed(
        target_customer_ref=CUSTOMER_TELEGRAM_ID,
        enabled=True,
        acting_user=SELLER_TELEGRAM_ID,
    )

    assert isinstance(result, User)
    assert result.offline_payment_allowed is True

    # And it can be disabled again.
    disabled = service.set_offline_payment_allowed(
        target_customer_ref=CUSTOMER_TELEGRAM_ID,
        enabled=False,
        acting_user=SELLER_TELEGRAM_ID,
    )
    assert isinstance(disabled, User)
    assert disabled.offline_payment_allowed is False


def test_set_offline_payment_allowed_unknown_customer_not_found(service):
    result = service.set_offline_payment_allowed(
        target_customer_ref=123456789,  # no such customer
        enabled=True,
        acting_user=SELLER_TELEGRAM_ID,
    )
    assert isinstance(result, NotFound)
    assert result.entity == "customer"


def test_set_offline_payment_allowed_non_seller_not_authorized(service, db):
    service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )

    result = service.set_offline_payment_allowed(
        target_customer_ref=CUSTOMER_TELEGRAM_ID,
        enabled=True,
        acting_user=CUSTOMER_TELEGRAM_ID,  # not the Seller
    )

    assert isinstance(result, NotAuthorized)
    # Flag unchanged.
    stored = next(iter(db.users.values()))
    assert stored.offline_payment_allowed is False


def test_set_offline_payment_allowed_resolves_by_verified_contact(service):
    service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )
    result = service.set_offline_payment_allowed(
        target_customer_ref="+919812345678",  # by Verified_Contact (Req 17.1)
        enabled=True,
        acting_user=SELLER_TELEGRAM_ID,
    )
    assert isinstance(result, User)
    assert result.offline_payment_allowed is True


# --------------------------------------------------------------------------- #
# set_language_preference (Req 18.4, 18.5, 18.8, 18.2)
# --------------------------------------------------------------------------- #
def test_set_language_preference_round_trip_for_customer(service):
    service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )

    result = service.set_language_preference(CUSTOMER_TELEGRAM_ID, "EN")
    assert isinstance(result, User)
    assert result.language_preference is Language.EN

    # Round-trip: a subsequent identify returns the stored language.
    again = service.identify(CUSTOMER_TELEGRAM_ID)
    assert isinstance(again, User)
    assert again.language_preference is Language.EN


def test_set_language_preference_round_trip_for_seller(service):
    # The Seller shares their own contact, then switches language (Req 18.8).
    service.register_contact(
        SELLER_TELEGRAM_ID,
        SharedContact(phone_number="+919800000000", user_id=SELLER_TELEGRAM_ID),
    )

    result = service.set_language_preference(SELLER_TELEGRAM_ID, Language.HI)
    assert isinstance(result, User)
    assert result.role is Role.ADMIN  # role-independent control still works
    assert result.language_preference is Language.HI

    again = service.identify(SELLER_TELEGRAM_ID)
    assert isinstance(again, User)
    assert again.language_preference is Language.HI


def test_set_language_preference_unknown_user_not_found(service):
    result = service.set_language_preference(987654321, "EN")
    assert isinstance(result, NotFound)
    assert result.entity == "user"


def test_set_language_preference_invalid_language_rejected(service):
    service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )
    result = service.set_language_preference(CUSTOMER_TELEGRAM_ID, "FR")
    assert isinstance(result, Rejected)
    assert result.code == CODE_INVALID_LANGUAGE


def test_unset_preference_defaults_to_hindi(service):
    registered = service.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )
    # A freshly registered user has no stored preference.
    assert registered.language_preference is None
    # The renderer-facing helper resolves the unset preference to Hindi (Req 18.2).
    assert effective_language(registered.language_preference) is Language.HI
    assert effective_language(Language.EN) is Language.EN
