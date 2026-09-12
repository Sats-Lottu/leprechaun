from enum import StrEnum, auto


class CheckoutSessionStatus(StrEnum):
    CREATED: str = auto()
    AWAITING_PAYMENT: str = auto()
    RESERVED: str = auto()
    SETTLED: str = auto()
    FAILED: str = auto()
    CANCELED: str = auto()
    EXPIRED: str = auto()


class AdminRoleStatus(StrEnum):
    ACTIVE: str = auto()
    REVOKED: str = auto()
