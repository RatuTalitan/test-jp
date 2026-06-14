"""Payment_Service subsystem.

Presents Seller-configured UPI details (address + QR), records the Customer's
UTR against a configurable pattern with global uniqueness, and stores an
optional payment screenshot reference (blob in object storage, only the key in
the DB). Language-agnostic: returns typed results with stable status codes.

Clean importable API::

    from marketplace.payment import (
        PaymentService,
        UpiInstructions,
        ObjectStore,
        InMemoryObjectStore,
    )
"""

from marketplace.payment.object_store import (
    InMemoryObjectStore,
    ObjectStore,
    StoredObject,
)
from marketplace.payment.service import (
    DUPLICATE_UTR,
    IMAGE_TOO_LARGE,
    INVALID_UTR_FORMAT,
    MAX_SCREENSHOT_BYTES,
    PAYMENT_NOT_PENDING,
    PAYMENT_SUBMITTED_TRANSITION_SEQ,
    SUPPORTED_IMAGE_CONTENT_TYPES,
    UNSUPPORTED_IMAGE_FORMAT,
    UPI_NOT_CONFIGURED,
    PaymentService,
    UpiInstructions,
)

__all__ = [
    "PaymentService",
    "UpiInstructions",
    "ObjectStore",
    "InMemoryObjectStore",
    "StoredObject",
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
]
