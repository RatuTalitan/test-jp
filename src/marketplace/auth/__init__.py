"""Auth_Service subsystem.

Establishes and stores a user's Verified_Contact (Telegram Share Contact),
identifies returning users, assigns roles (Admin only to the configured Seller),
manages the Offline_Payment_Allowed flag and the per-user Language_Preference.
Language-agnostic: returns typed results and stable status/error codes.

The public API (task 6.1) lives in :mod:`marketplace.auth.service` and is
re-exported here so the Admin_Console and Bot_Interface can simply
``from marketplace.auth import AuthService, SharedContact, OK``.
"""

from marketplace.auth.service import (
    CODE_CONTACT_IDENTITY_MISMATCH,
    CODE_IDENTIFICATION_FAILED,
    CODE_INVALID_LANGUAGE,
    OK,
    AuthService,
    Ok,
    SharedContact,
    effective_language,
)

__all__ = [
    "AuthService",
    "SharedContact",
    "Ok",
    "OK",
    "effective_language",
    "CODE_CONTACT_IDENTITY_MISMATCH",
    "CODE_IDENTIFICATION_FAILED",
    "CODE_INVALID_LANGUAGE",
]
