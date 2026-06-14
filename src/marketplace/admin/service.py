"""Admin_Console: Seller entry points for settings, verifications, and active-order screens.

This is the channel- and DB-agnostic **Admin_Console** described in design.md ->
``Admin_Console``. Like the other domain services it reaches persistence **only**
through the repository protocols exposed by a
:class:`~marketplace.domain.repositories.UnitOfWork` and never imports the ORM or
opens a connection itself (design.md -> Architectural Principles). The caller
(the Bot_Interface) owns the transaction boundary; this service performs its work
inside that transaction and never commits on its own.

Scope of task 20.2 (design.md -> Admin_Console; Req 19):

* ``set_pickup_location(text, acting_user)`` -- persist the Seller's
  Pickup_Location (1-500 chars) into the single-row ``seller_settings`` (Req
  19.1/19.2).
* ``set_upi_address(vpa, acting_user)`` -- persist the Seller's UPI_Address after
  validating the VPA shape (non-empty local part + a single ``@`` + non-empty
  handle) (Req 19.3/19.4).
* ``set_upi_qr(image_bytes, content_type, size, acting_user)`` -- validate a
  supported image within the 10 MB cap, store the blob via the
  :class:`~marketplace.payment.object_store.ObjectStore` and persist **only** the
  returned object key into ``seller_settings.upi_qr_object_key`` (Req 19.5/19.6).
* ``mark_ready(order_id, acting_user)`` -- a thin, non-blocking wrapper over
  :meth:`marketplace.order.service.OrderService.mark_ready` that, when no
  Pickup_Location is configured, attaches a **warning** flag while still
  performing the APPROVED -> READY_FOR_PICKUP transition (Req 19.9). Also
  attaches a fulfillability shortfall summary to the result for informational
  purposes (never blocks the transition, Req 16.5).

Scope of task 20.1 (design.md -> Admin_Console; Req 7.1, 10.6, 16.3-16.5, 17.2):

* ``list_pending_verifications(acting_user)`` -- Seller-only; lists every Order in
  the PAYMENT_SUBMITTED state with its UTR, total amount, any screenshot key, and
  a fulfillability flag (Req 7.1, 16.3/16.4). The flag NEVER blocks approval
  (Req 16.5).
* ``list_active_orders(acting_user)`` -- Seller-only; lists all non-terminal
  orders (delegates to OrderService.list_active_for_seller()) enriched with the
  same fulfillability data (Req 10.6, 16.3/16.4/16.5).
* ``set_offline_payment_allowed(target_customer_ref, enabled, acting_user)`` --
  Seller-only; delegates to Auth_Service.set_offline_payment_allowed (Req 17.2).

Security: every entry point first calls ``Auth_Service.require_admin`` (Req 1.7,
12.1, 17.2, 19.8); a non-Seller is rejected with
:class:`~marketplace.domain.results.NotAuthorized` and no data is changed.
Following the "stable status codes, never localized prose" rule, validation
refusals are returned as typed :class:`~marketplace.domain.results.Rejected`
results carrying a **stable code** plus structured ``details`` the Bot_Interface
maps to a localized Message_Catalog template (Req 18).

Clean importable API::

    from marketplace.admin import (
        AdminConsole,
        ReadyResult,
        SellerOrderView,
        PICKUP_LOCATION_INVALID,
        INVALID_VPA,
        UNSUPPORTED_QR_FORMAT,
        QR_IMAGE_TOO_LARGE,
        PICKUP_LOCATION_NOT_SET,
        SUPPORTED_QR_IMAGE_CONTENT_TYPES,
        MAX_QR_IMAGE_BYTES,
    )

Requirements: 7.1, 10.6, 12.1, 16.3, 16.4, 16.5, 17.2,
              19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union

from marketplace.auth.service import AuthService
from marketplace.domain.entities import Order, OrderState, SellerSettings, User, new_id
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import Failure, NotAuthorized, NotFound, Rejected
from marketplace.fulfillability.engine import (
    Shortfall,
    _stock_snapshot_for_order,
    is_fulfillable,
    shortfalls as _compute_shortfalls,
)
from marketplace.order.service import OrderService
from marketplace.order.state_machine import Actor
from marketplace.payment.object_store import ObjectStore

__all__ = [
    "AdminConsole",
    "ReadyResult",
    "SellerOrderView",
    # stable status codes
    "PICKUP_LOCATION_INVALID",
    "INVALID_VPA",
    "UNSUPPORTED_QR_FORMAT",
    "QR_IMAGE_TOO_LARGE",
    "PICKUP_LOCATION_NOT_SET",
    # validation policy
    "PICKUP_LOCATION_MAX_CHARS",
    "SUPPORTED_QR_IMAGE_CONTENT_TYPES",
    "MAX_QR_IMAGE_BYTES",
    # result aliases
    "SettingsResult",
    "OfflinePaymentResult",
]

# --- Stable status codes (language-agnostic; localized by the Bot_Interface) --
#: A submitted Pickup_Location is empty or exceeds the 500-char cap (Req 19.2).
#: ``details`` carries the accepted length so the message can state the
#: requirement. The current Pickup_Location is left unchanged.
PICKUP_LOCATION_INVALID = "PICKUP_LOCATION_INVALID"
#: A submitted UPI_Address does not satisfy the VPA shape -- a non-empty local
#: part, a single ``@``, then a non-empty handle (Req 19.4). The current
#: UPI_Address is left unchanged and the response states the required format.
INVALID_VPA = "INVALID_VPA"
#: A UPI_QR_Code upload is in an unsupported image format (Req 19.6). ``details``
#: lists the accepted formats + size cap; the current QR is left unchanged.
UNSUPPORTED_QR_FORMAT = "UNSUPPORTED_QR_FORMAT"
#: A UPI_QR_Code upload exceeds the 10 MB cap (Req 19.6). ``details`` lists the
#: accepted formats + size cap; the current QR is left unchanged.
QR_IMAGE_TOO_LARGE = "QR_IMAGE_TOO_LARGE"
#: The Seller marked an order ready while no Pickup_Location is configured (Req
#: 19.9). This is a **non-blocking warning** carried on a successful
#: :class:`ReadyResult` -- the transition still happens; it is never a failure.
PICKUP_LOCATION_NOT_SET = "PICKUP_LOCATION_NOT_SET"


#: Pickup_Location length bounds (Req 19.1/19.2): 1 to 500 characters inclusive.
PICKUP_LOCATION_MAX_CHARS: int = 500

#: Supported UPI_QR_Code image formats (Req 19.5/19.6). The same small set of
#: common, browser-renderable raster formats accepted for payment screenshots;
#: an upload outside this set is rejected with the accepted formats stated.
SUPPORTED_QR_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)

#: Maximum accepted UPI_QR_Code image size: 10 MB (Req 19.5/19.6), expressed in
#: bytes using the binary mebibyte (10 * 1024 * 1024) so the boundary is exact.
MAX_QR_IMAGE_BYTES: int = 10 * 1024 * 1024


#: A settings mutation returns the updated row or a typed failure.
SettingsResult = Union[SellerSettings, Rejected, NotAuthorized]

#: ``set_offline_payment_allowed`` delegates to Auth_Service and returns the
#: updated :class:`User`, or a ``NotFound`` / ``NotAuthorized`` on failure (Req 17.2).
OfflinePaymentResult = Union[User, NotFound, NotAuthorized]


@dataclass(frozen=True)
class SellerOrderView:
    """A Seller-facing snapshot of one Order with payment details and fulfillability.

    Returned by :meth:`AdminConsole.list_pending_verifications` and
    :meth:`AdminConsole.list_active_orders`. Each entry bundles:

    * ``order`` -- the full :class:`~marketplace.domain.entities.Order` entity
      (line items, state, total, timestamps).
    * ``utr`` -- the UTR recorded against this order's payment (``None`` when
      no payment has been submitted yet, e.g. for PLACED/PAYMENT_PENDING orders
      in the active-orders list).
    * ``screenshot_key`` -- the object-storage key for any attached payment
      screenshot (``None`` if none uploaded).
    * ``fulfillable`` -- ``True`` iff every line item's ordered quantity is
      ``<=`` the corresponding Product's current stock quantity (Req 16.2).
      The flag is **informational only** and NEVER blocks the Seller from
      approving or acting on the order (Req 16.5).
    * ``shortfalls`` -- a tuple of :class:`~marketplace.fulfillability.engine.Shortfall`
      entries for every line that cannot be met from current stock (empty when
      ``fulfillable`` is ``True``).  Carries enough detail for the Bot_Interface
      to render the per-line shortfall identified in Req 16.3.
    """

    order: object  # Order entity
    utr: Optional[str]
    screenshot_key: Optional[str]
    fulfillable: bool
    shortfalls: tuple = ()


@dataclass(frozen=True)
class ReadyResult:
    """The outcome of an Admin_Console :meth:`AdminConsole.mark_ready` (Req 19.9).

    Bundles the transitioned :class:`~marketplace.domain.entities.Order` with a
    **non-blocking** Pickup_Location warning. When the Seller marks an order
    ready while no Pickup_Location is configured, ``pickup_location_warning`` is
    ``True`` and ``warning_code`` is :data:`PICKUP_LOCATION_NOT_SET` -- but the
    transition still happened, so the warning never hard-blocks the action. When
    a Pickup_Location is set, ``pickup_location_warning`` is ``False`` and
    ``warning_code`` is ``None``.

    Additionally carries a fulfillability snapshot computed at mark-ready time
    (optional enhancement, Req 16.3/16.5). ``fulfillable`` and
    ``fulfillability_shortfalls`` are informational only -- they NEVER block the
    Seller from marking an order ready (Req 16.5).
    """

    order: object
    pickup_location_warning: bool = False
    warning_code: Optional[str] = None
    #: ``True`` iff every line can be met from current stock at mark-ready time.
    fulfillable: bool = True
    #: Non-empty when at least one line's ordered qty exceeds current stock.
    fulfillability_shortfalls: tuple = ()


#: ``mark_ready`` returns the ``ReadyResult`` on a successful transition, or the
#: state-preserving failure the underlying OrderService/state machine produced
#: (wrong state, wrong actor, not authorized, not found).
ReadyOutcome = Union[ReadyResult, Failure]


class AdminConsole:
    """Seller-only settings entry points bound to one :class:`UnitOfWork`.

    Constructed with the active ``UnitOfWork``, an :class:`AuthService` (for the
    ``require_admin`` gate), and -- for ``set_upi_qr`` -- an
    :class:`~marketplace.payment.object_store.ObjectStore`. An optional
    ``order_service`` lets the mark-ready wrapper reuse an existing
    :class:`~marketplace.order.service.OrderService`; by default one is
    constructed over the same Unit-of-Work so both participate in the same
    transaction. Uses the ``seller_settings`` repository (``get``/``upsert``).
    Mutating operations leave the transaction open for the caller to commit; a
    rejected operation makes no persistent change.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        auth_service: AuthService,
        object_store: Optional[ObjectStore] = None,
        order_service: Optional[OrderService] = None,
    ) -> None:
        self._uow = uow
        self._auth = auth_service
        self._object_store = object_store
        self._order_service = order_service

    # ------------------------------------------------------ set_pickup_location
    def set_pickup_location(self, text, acting_user) -> SettingsResult:
        """Persist the Seller's Pickup_Location (Req 19.1/19.2/19.8).

        Gating first (Req 19.8): a non-Seller ``acting_user`` is rejected with
        :class:`~marketplace.domain.results.NotAuthorized` and the current
        Pickup_Location is left unchanged. Validation (Req 19.2): ``text`` must be
        1-500 characters; anything empty/``None`` or longer is rejected with
        :data:`PICKUP_LOCATION_INVALID` (current value unchanged). On success the
        Pickup_Location is upserted into the single ``seller_settings`` row and
        the updated :class:`~marketplace.domain.entities.SellerSettings` returned
        (Req 19.1).
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        if text is None or not (1 <= len(text) <= PICKUP_LOCATION_MAX_CHARS):
            return Rejected(
                PICKUP_LOCATION_INVALID,
                reason="the Pickup_Location must be 1 to 500 characters",
                details={"min_chars": 1, "max_chars": PICKUP_LOCATION_MAX_CHARS},
            )

        settings = self._load_or_new_settings()
        settings.pickup_location = text
        return self._persist(settings)

    # --------------------------------------------------------- set_upi_address
    def set_upi_address(self, vpa, acting_user) -> SettingsResult:
        """Persist the Seller's UPI_Address after VPA validation (Req 19.3/19.4/19.8).

        Gating first (Req 19.8): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized`, leaving the current
        UPI_Address unchanged. Validation (Req 19.4): the VPA must be a non-empty
        local part, a single ``@`` character, then a non-empty handle; anything
        else is rejected with :data:`INVALID_VPA` stating the required format
        (current value unchanged). On success the UPI_Address is upserted into the
        single ``seller_settings`` row and the updated settings returned (Req
        19.3). Because ``Payment_Service.present_instructions`` reads this same
        row, the new address immediately flows into customer payment instructions
        (Req 19.7).
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        if not self._is_valid_vpa(vpa):
            return Rejected(
                INVALID_VPA,
                reason=(
                    "the UPI_Address must be a non-empty local part, a single "
                    "'@', then a non-empty handle"
                ),
                details={"format": "local@handle"},
            )

        settings = self._load_or_new_settings()
        settings.upi_address = vpa
        return self._persist(settings)

    # ------------------------------------------------------------- set_upi_qr
    def set_upi_qr(
        self,
        image_bytes: bytes,
        content_type: str,
        size: int,
        acting_user,
    ) -> SettingsResult:
        """Store a UPI_QR_Code image and persist only its object key (Req 19.5/19.6/19.8).

        Gating first (Req 19.8): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized`, leaving the current QR
        unchanged. Validation (Req 19.6): a supported image format within the
        10 MB cap; an unsupported format yields :data:`UNSUPPORTED_QR_FORMAT` and
        an oversized blob yields :data:`QR_IMAGE_TOO_LARGE`, each stating the
        accepted format + size and leaving the current QR unchanged. On success
        the blob is stored via the :class:`ObjectStore` and **only** the returned
        object key is saved into ``seller_settings.upi_qr_object_key`` -- the
        image bytes never reach the DB (Req 19.5). Because
        ``Payment_Service.present_instructions`` reads this same row, the new QR
        reference immediately flows into customer payment instructions (Req 19.7).

        Args:
            image_bytes: the QR image bytes (or a provider file reference's bytes).
            content_type: the declared image MIME type (e.g. ``image/png``).
            size: the upload size in bytes (validated against the cap).
            acting_user: the Telegram user id (int) or :class:`User` performing
                the action; must be the Seller.
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        if self._object_store is None:
            raise ValueError(
                "set_upi_qr requires an ObjectStore; construct "
                "AdminConsole(uow, auth_service, object_store=...)"
            )

        accepted = {
            "accepted_formats": sorted(SUPPORTED_QR_IMAGE_CONTENT_TYPES),
            "max_bytes": MAX_QR_IMAGE_BYTES,
        }

        # Format check (Req 19.6).
        if content_type not in SUPPORTED_QR_IMAGE_CONTENT_TYPES:
            return Rejected(
                UNSUPPORTED_QR_FORMAT,
                reason="the UPI_QR_Code is not a supported image format",
                details={"content_type": content_type, **accepted},
            )

        # Size check (Req 19.6): the boundary itself (exactly 10 MB) is allowed.
        if size is None or size < 0 or size > MAX_QR_IMAGE_BYTES:
            return Rejected(
                QR_IMAGE_TOO_LARGE,
                reason="the UPI_QR_Code exceeds the maximum allowed size",
                details={"size": size, **accepted},
            )

        # Store the blob; only the returned key is persisted in the DB.
        key = self._qr_key(content_type)
        stored_key = self._object_store.put(key, image_bytes, content_type)

        settings = self._load_or_new_settings()
        settings.upi_qr_object_key = stored_key
        return self._persist(settings)

    # -------------------------------------------------------------- mark_ready
    def mark_ready(self, order_id, acting_user) -> ReadyOutcome:
        """Non-blocking Seller "mark ready" with a Pickup_Location warning (Req 19.9).

        Gating first (Req 19.8/12.1): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized` and nothing changes.
        Otherwise the APPROVED -> READY_FOR_PICKUP transition is performed by the
        **existing** :meth:`OrderService.mark_ready` (this wrapper never modifies
        the Order_Service). If the transition succeeds and **no** Pickup_Location
        is configured, the returned :class:`ReadyResult` carries
        ``pickup_location_warning=True`` / ``warning_code=PICKUP_LOCATION_NOT_SET``
        -- a warning that the Seller should set a Pickup_Location -- yet the
        transition still happened, so the warning never hard-blocks the action
        (Req 19.9). A state-preserving failure from the state machine (wrong
        state/actor, not found) is returned unchanged.

        Additionally attaches a fulfillability shortfall summary to the
        :class:`ReadyResult` (informational; never blocks the action, Req 16.5).
        The snapshot is computed from the current catalog after the transition so
        the Seller has an up-to-date view of whether the stock is still present.
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        order_service = self._order_service or OrderService(self._uow)
        actor = self._seller_actor(acting_user)
        result = order_service.mark_ready(order_id, actor)

        # A failure (wrong state/actor/not found) leaves the order unchanged;
        # surface it as-is (no warning is attached to a non-transition).
        if not isinstance(result, Order):
            return result

        # Compute fulfillability snapshot at mark-ready time (Req 16.3/16.5):
        # informational only, never blocks the transition.
        snapshot = _stock_snapshot_for_order(self._uow, result)
        fulfillable = is_fulfillable(result, snapshot)
        shortfall_list = tuple(_compute_shortfalls(result, snapshot))

        settings = self._uow.seller_settings.get()
        pickup_unset = settings is None or not settings.pickup_location
        if pickup_unset:
            return ReadyResult(
                order=result,
                pickup_location_warning=True,
                warning_code=PICKUP_LOCATION_NOT_SET,
                fulfillable=fulfillable,
                fulfillability_shortfalls=shortfall_list,
            )
        return ReadyResult(
            order=result,
            pickup_location_warning=False,
            fulfillable=fulfillable,
            fulfillability_shortfalls=shortfall_list,
        )

    # ----------------------------------------- list_pending_verifications
    def list_pending_verifications(self, acting_user) -> Union[list, NotAuthorized]:
        """List every PAYMENT_SUBMITTED order with payment details and fulfillability (Req 7.1, 16.3, 16.4).

        Gating first (Req 12.1): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized` and no data is read.
        On success returns a list of :class:`SellerOrderView` entries, one per
        order in the PAYMENT_SUBMITTED state, each enriched with:

        * the order's UTR and any payment screenshot key (read from the
          ``payments`` repository per order, Req 7.1);
        * a ``fulfillable`` flag and per-line ``shortfalls`` computed from the
          **current** catalog stock (Req 16.3/16.4).

        The flag is **informational only** -- it NEVER blocks the Seller from
        approving an order (Req 16.5). The approval-time stock check in
        :meth:`marketplace.order.service.OrderService.approve` (Req 7.3/7.4)
        governs the actual stock decrement and prevents overselling.
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        orders = self._uow.orders.list_by_states([OrderState.PAYMENT_SUBMITTED])
        return [self._build_seller_order_view(order) for order in orders]

    # --------------------------------------------------- list_active_orders
    def list_active_orders(self, acting_user) -> Union[list, NotAuthorized]:
        """List all non-terminal orders enriched with fulfillability data (Req 10.6, 16.3-16.5).

        Gating first (Req 12.1): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized` and no data is read.
        On success delegates to
        :meth:`marketplace.order.service.OrderService.list_active_for_seller`
        (which returns non-terminal orders oldest-first, Req 10.6) and enriches
        each order with the same :class:`SellerOrderView` shape as
        :meth:`list_pending_verifications` (Req 16.3/16.4). The fulfillability
        flag NEVER blocks any Seller action (Req 16.5).
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        order_service = self._order_service or OrderService(self._uow)
        orders = order_service.list_active_for_seller()
        return [self._build_seller_order_view(order) for order in orders]

    # --------------------------------------- set_offline_payment_allowed
    def set_offline_payment_allowed(
        self,
        target_customer_ref,
        enabled: bool,
        acting_user,
    ) -> OfflinePaymentResult:
        """Enable/disable a Customer's Offline_Payment_Allowed flag (Req 17.1/17.2/17.3).

        Gating first (Req 17.2/12.1): a non-Seller is rejected with
        :class:`~marketplace.domain.results.NotAuthorized` and no change is made.
        On success delegates entirely to
        :meth:`marketplace.auth.service.AuthService.set_offline_payment_allowed`,
        which persists the new flag value and returns the updated
        :class:`~marketplace.domain.entities.User`. An unknown
        ``target_customer_ref`` yields :class:`~marketplace.domain.results.NotFound`
        (Req 17.3).

        Args:
            target_customer_ref: the Customer identified by internal ``user_id``
                (UUID), Telegram user id (int), or ``Verified_Contact`` (str).
            enabled: the new flag value (``True`` enables, ``False`` disables).
            acting_user: the Telegram user id (int) or :class:`User` performing
                the action; must be the Seller.
        """
        gate = self._require_admin(acting_user)
        if gate is not None:
            return gate

        return self._auth.set_offline_payment_allowed(
            target_customer_ref, enabled, acting_user
        )

    # ------------------------------------------------------------------ internals
    def _build_seller_order_view(self, order: Order) -> SellerOrderView:
        """Build a :class:`SellerOrderView` for one order.

        Reads the order's payment record (if any) for the UTR and screenshot key,
        then computes fulfillability from the current catalog stock via the
        Fulfillability Engine's pure functions (``is_fulfillable`` /
        ``shortfalls``). The stock snapshot is built on read so it is never
        stale (Req 16.1). The flag is informational only and NEVER gates any
        Seller action (Req 16.5).
        """
        payment = self._uow.payments.get_by_order(order.order_id)
        utr = payment.utr if payment is not None else None
        screenshot_key = (
            payment.screenshot_object_key if payment is not None else None
        )

        snapshot = _stock_snapshot_for_order(self._uow, order)
        fulfillable = is_fulfillable(order, snapshot)
        shortfall_list = tuple(_compute_shortfalls(order, snapshot))

        return SellerOrderView(
            order=order,
            utr=utr,
            screenshot_key=screenshot_key,
            fulfillable=fulfillable,
            shortfalls=shortfall_list,
        )

    def _require_admin(self, acting_user) -> Optional[NotAuthorized]:
        """Run the Seller gate; return :class:`NotAuthorized` to reject, else ``None``.

        Centralizes the Req 19.8/1.7/12.1 check every entry point performs first.
        Accepts either the raw Telegram user id (int) or a
        :class:`~marketplace.domain.entities.User` and delegates to
        ``Auth_Service.require_admin`` (config-driven, no transaction needed).
        """
        telegram_id = self._telegram_id_of(acting_user)
        outcome = self._auth.require_admin(telegram_id)
        if isinstance(outcome, NotAuthorized):
            return outcome
        return None

    def _load_or_new_settings(self) -> SellerSettings:
        """Return the current settings row (to update) or a fresh seeded one.

        Reads the singleton ``seller_settings`` row via the repository; when none
        has been seeded yet a new :class:`SellerSettings` (carrying the default
        ``utr_pattern``) is created so the first setter materializes the row. The
        returned object is the repository's detached copy, so mutating it and
        upserting preserves every **other** field (e.g. an unrelated
        ``utr_pattern`` or ``pickup_location``) untouched.
        """
        existing = self._uow.seller_settings.get()
        if existing is not None:
            return existing
        return SellerSettings(seller_settings_id=new_id())

    def _persist(self, settings: SellerSettings) -> SellerSettings:
        """Stamp ``updated_at`` and upsert the single settings row."""
        settings.updated_at = datetime.now(timezone.utc)
        return self._uow.seller_settings.upsert(settings)

    def _seller_actor(self, acting_user) -> Actor:
        """Build a Seller :class:`Actor` for the order state machine.

        ``mark_ready`` is role-gated (not ownership-gated), so the actor only
        needs the SELLER role; the user id is included when a
        :class:`User` is supplied so the actor is fully attributed.
        """
        if isinstance(acting_user, User):
            return Actor.seller(acting_user.user_id)
        return Actor.seller()

    @staticmethod
    def _telegram_id_of(acting_user) -> int:
        """Resolve ``acting_user`` (a :class:`User` or a Telegram id) to an int."""
        if isinstance(acting_user, User):
            return acting_user.telegram_user_id
        return int(acting_user)

    @staticmethod
    def _is_valid_vpa(vpa) -> bool:
        """Validate the VPA shape: non-empty local + single ``@`` + non-empty handle.

        Implements exactly the Req 19.3/19.4 definition -- a single ``@`` splitting
        a non-empty local part from a non-empty handle. No other characters are
        constrained (the design defines the VPA only by this shape).
        """
        if not isinstance(vpa, str):
            return False
        if vpa.count("@") != 1:
            return False
        local, _, handle = vpa.partition("@")
        return len(local) > 0 and len(handle) > 0

    @staticmethod
    def _qr_key(content_type: str) -> str:
        """Build a unique private object-storage key for the UPI QR image blob."""
        ext = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }.get(content_type, "bin")
        return f"upi-qr/{new_id()}.{ext}"
