"""Admin_Console subsystem.

Seller-only entry points (gated by ``Auth_Service.require_admin``) for catalog
management, pending verifications, approvals, fulfillment transitions, order
modification, the offline-payment flag, active-order listing, and Seller
settings (pickup location + UPI details). Language-agnostic.

The Seller-settings API (task 20.2) and the Seller screens (task 20.1) live in
:mod:`marketplace.admin.service` and are re-exported here so callers can simply
``from marketplace.admin import AdminConsole, ReadyResult, SellerOrderView``.
"""

from marketplace.admin.service import (
    INVALID_VPA,
    MAX_QR_IMAGE_BYTES,
    PICKUP_LOCATION_INVALID,
    PICKUP_LOCATION_MAX_CHARS,
    PICKUP_LOCATION_NOT_SET,
    QR_IMAGE_TOO_LARGE,
    SUPPORTED_QR_IMAGE_CONTENT_TYPES,
    UNSUPPORTED_QR_FORMAT,
    AdminConsole,
    OfflinePaymentResult,
    ReadyResult,
    SellerOrderView,
    SettingsResult,
)

__all__ = [
    "AdminConsole",
    "ReadyResult",
    "SellerOrderView",
    "SettingsResult",
    "OfflinePaymentResult",
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
]
