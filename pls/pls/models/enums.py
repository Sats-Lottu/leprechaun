from enum import StrEnum, auto


class PaymentProvider(StrEnum):
    # External Lightning provider integrated by this service.
    LNBITS: str = auto()


class PaymentDirection(StrEnum):
    # Incoming payment to the platform wallet.
    INCOMING: str = auto()
    # Outgoing payment from the platform wallet.
    OUTGOING: str = auto()


class InvoiceStatus(StrEnum):
    # Invoice was created and is waiting for payment.
    PENDING: str = auto()
    # Provider confirmed the invoice as paid.
    PAID: str = auto()
    # Invoice expired before payment confirmation.
    EXPIRED: str = auto()
    # Invoice was cancelled by the platform or provider.
    CANCELLED: str = auto()
    # Invoice creation or tracking failed.
    FAILED: str = auto()


class ProviderEventStatus(StrEnum):
    # Raw provider event was stored but not handled yet.
    RECEIVED: str = auto()
    # Event was converted into internal effects/events.
    PROCESSED: str = auto()
    # Event handling failed and needs retry or inspection.
    FAILED: str = auto()


class OperationStatus(StrEnum):
    # Operation started but did not reach a final state yet.
    STARTED: str = auto()
    # Operation completed successfully.
    SUCCEEDED: str = auto()
    # Operation reached a final failed state.
    FAILED: str = auto()


class OutboxStatus(StrEnum):
    # Event was stored in the same transaction as the state change.
    PENDING: str = auto()
    # Event was published to RabbitMQ.
    PUBLISHED: str = auto()
    # Event reached a final failed state after operator intervention.
    FAILED: str = auto()
