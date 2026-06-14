"""Domain layer.

Channel-neutral domain entities, typed result objects (Rejected / NotFound /
NotAuthorized / Unauthenticated / Conflict), the repository protocols and their
in-memory implementation, the Order_State definitions, the Monetary_Rounding
utility, and versioned serialization shared across services. Contains no
Telegram SDK imports and no I/O.

Task 4.1 adds the domain entities, the typed result vocabulary, the repository
protocols + Unit-of-Work contract, and the fast in-memory repository
implementation used by the property/unit tests. The SQLAlchemy-backed
implementation of the same protocols lives in :mod:`marketplace.db.repositories`.
"""

from marketplace.domain.entities import (
    AuditAction,
    AuditEntry,
    Cart,
    CartItem,
    Category,
    DEFAULT_UTR_PATTERN,
    Language,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import (
    InMemoryAuditRepository,
    InMemoryCartRepository,
    InMemoryCatalogRepository,
    InMemoryDatabase,
    InMemoryNotificationRepository,
    InMemoryOrderRepository,
    InMemoryPaymentRepository,
    InMemorySellerSettingsRepository,
    InMemoryUnitOfWork,
    InMemoryUserRepository,
)
from marketplace.domain.repositories import (
    AuditRepository,
    CartRepository,
    CatalogRepository,
    NotificationRepository,
    OrderRepository,
    PaymentRepository,
    SellerSettingsRepository,
    UnitOfWork,
    UserRepository,
)
from marketplace.domain.results import (
    Conflict,
    Failure,
    NotAuthorized,
    NotFound,
    Rejected,
    Unauthenticated,
    is_failure,
)
from marketplace.domain.serialization import (
    FORMAT_VERSION,
    FORMAT_VERSION_KEY,
    dumps,
    ensure_versioned_document,
    from_dict,
    loads,
    to_dict,
)

__all__ = [
    # entities + enums
    "User",
    "Category",
    "Product",
    "Cart",
    "CartItem",
    "Order",
    "OrderItem",
    "Payment",
    "AuditEntry",
    "Notification",
    "SellerSettings",
    "Role",
    "Unit",
    "OrderState",
    "Language",
    "AuditAction",
    "NotificationKind",
    "NotificationStatus",
    "DEFAULT_UTR_PATTERN",
    "new_id",
    # results
    "Rejected",
    "NotFound",
    "NotAuthorized",
    "Unauthenticated",
    "Conflict",
    "Failure",
    "is_failure",
    # repository protocols
    "UserRepository",
    "CatalogRepository",
    "CartRepository",
    "OrderRepository",
    "PaymentRepository",
    "AuditRepository",
    "NotificationRepository",
    "SellerSettingsRepository",
    "UnitOfWork",
    # in-memory implementations
    "InMemoryDatabase",
    "InMemoryUnitOfWork",
    "InMemoryUserRepository",
    "InMemoryCatalogRepository",
    "InMemoryCartRepository",
    "InMemoryOrderRepository",
    "InMemoryPaymentRepository",
    "InMemoryAuditRepository",
    "InMemoryNotificationRepository",
    "InMemorySellerSettingsRepository",
    # versioned serialization
    "FORMAT_VERSION",
    "FORMAT_VERSION_KEY",
    "to_dict",
    "from_dict",
    "dumps",
    "loads",
    "ensure_versioned_document",
]
