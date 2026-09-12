from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, registry, relationship

from pls.models.enums import (
    InvoiceStatus,
    OperationStatus,
    OutboxStatus,
    PaymentDirection,
    PaymentProvider,
    ProviderEventStatus,
)

table_registry = registry()


def _enum_values(enum_cls: type) -> str:
    return ", ".join(f"'{item.value}'" for item in enum_cls)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), init=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@table_registry.mapped_as_dataclass
class LightningInvoice(TimestampMixin):
    __tablename__ = "lightning_invoices"
    __table_args__ = (
        CheckConstraint(
            "amount_msat > 0",
            name="ck_lightning_invoices_amount_msat_positive",
        ),
        CheckConstraint(
            f"provider IN ({_enum_values(PaymentProvider)})",
            name="ck_lightning_invoices_provider_valid",
        ),
        CheckConstraint(
            f"direction IN ({_enum_values(PaymentDirection)})",
            name="ck_lightning_invoices_direction_valid",
        ),
        CheckConstraint(
            f"status IN ({_enum_values(InvoiceStatus)})",
            name="ck_lightning_invoices_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    # Internal user that requested or owns the invoice.
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    amount_msat: Mapped[int] = mapped_column()

    provider: Mapped[str] = mapped_column(
        String(40),
        default=PaymentProvider.LNBITS,
        index=True,
    )
    direction: Mapped[str] = mapped_column(
        String(40),
        default=PaymentDirection.INCOMING,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default=InvoiceStatus.PENDING,
        index=True,
    )

    memo: Mapped[str] = mapped_column(String(255), default="")

    # Provider identifiers returned by LNbits.
    provider_invoice_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        unique=True,
        default=None,
    )
    payment_hash: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
        default=None,
    )
    payment_request: Mapped[str] = mapped_column(String(4096), default="")

    idempotency_key: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        unique=True,
        default=None,
    )
    idempotency_payload_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        default=None,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    raw_response: Mapped[dict] = mapped_column(JSON, default_factory=dict)

    provider_events: Mapped[list["ProviderEvent"]] = relationship(
        back_populates="invoice",
        init=False,
        default_factory=list,
    )


@table_registry.mapped_as_dataclass
class ProviderEvent(TimestampMixin):
    __tablename__ = "provider_events"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_enum_values(PaymentProvider)})",
            name="ck_provider_events_provider_valid",
        ),
        CheckConstraint(
            f"status IN ({_enum_values(ProviderEventStatus)})",
            name="ck_provider_events_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    provider: Mapped[str] = mapped_column(
        String(40),
        default=PaymentProvider.LNBITS,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(120), default="", index=True
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default=ProviderEventStatus.RECEIVED,
        index=True,
    )

    invoice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("lightning_invoices.id"),
        nullable=True,
        index=True,
        default=None,
    )
    provider_invoice_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
        default=None,
    )
    payment_hash: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
        default=None,
    )

    payload: Mapped[dict] = mapped_column(JSON, default_factory=dict)
    error_detail: Mapped[str] = mapped_column(String(1000), default="")
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    invoice: Mapped["LightningInvoice"] = relationship(
        back_populates="provider_events",
        init=False,
    )


@table_registry.mapped_as_dataclass
class OperationIdempotency(TimestampMixin):
    __tablename__ = "operation_idempotencies"
    __table_args__ = (
        UniqueConstraint(
            "operation",
            "idempotency_key",
            name="uq_operation_idempotencies_operation_key",
        ),
        CheckConstraint(
            f"status IN ({_enum_values(OperationStatus)})",
            name="ck_operation_idempotencies_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    operation: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    resource_id: Mapped[UUID | None] = mapped_column(
        nullable=True,
        index=True,
        default=None,
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default=OperationStatus.STARTED,
        index=True,
    )
    payload_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )


@table_registry.mapped_as_dataclass
class OutboxEvent(TimestampMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_enum_values(OutboxStatus)})",
            name="ck_outbox_events_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    event_type: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(40),
        default=OutboxStatus.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    error_detail: Mapped[str] = mapped_column(String(1000), default="")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        default=None,
    )
