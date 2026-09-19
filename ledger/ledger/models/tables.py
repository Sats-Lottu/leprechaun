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

from ledger.models.enums import (
    AccountType,
    EntryType,
    HoldStatus,
    TransactionKind,
    TransactionStatus,
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
class Account(TimestampMixin):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "balance >= 0", name="ck_accounts_balance_non_negative"
        ),
        CheckConstraint(
            "reserved_balance >= 0",
            name="ck_accounts_reserved_balance_non_negative",
        ),
        CheckConstraint(
            "reserved_balance <= balance",
            name="ck_accounts_reserved_balance_not_greater_than_balance",
        ),
        CheckConstraint(
            f"account_type IN ({_enum_values(AccountType)})",
            name="ck_accounts_account_type_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    # tipo da conta contábil
    account_type: Mapped[str] = mapped_column(
        default=AccountType.USER,
        index=True,
    )
    # identifica quem é o "dono" dessa conta representado no domínio externo
    # ex: owner_id=<uuid do satoidc-service>
    owner_id: Mapped[UUID | None] = mapped_column(
        nullable=True, index=True, default=None
    )
    # opcional, útil para nome amigável
    name: Mapped[str] = mapped_column(default="")
    balance: Mapped[int] = mapped_column(default=0)
    reserved_balance: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="account",
        init=False,
        default_factory=list,
    )

    holds: Mapped[list["BalanceHold"]] = relationship(
        back_populates="account",
        init=False,
        default_factory=list,
    )


@table_registry.mapped_as_dataclass
class LedgerTransaction(TimestampMixin):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_enum_values(TransactionStatus)})",
            name="ck_ledger_transactions_status_valid",
        ),
        CheckConstraint(
            f"kind IN ({_enum_values(TransactionKind)})",
            name="ck_ledger_transactions_kind_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    status: Mapped[str] = mapped_column(
        default=TransactionStatus.PENDING,
        index=True,
    )

    kind: Mapped[str] = mapped_column(
        default=TransactionKind.TRANSFER,
        index=True,
    )

    external_origin: Mapped[str] = mapped_column(
        String(50), default="", index=True
    )

    # referência ao objeto de negócio que originou a transação
    # ex: payment_order, withdrawal, deposit
    reference_type: Mapped[str] = mapped_column(
        String(50), default="", index=True
    )
    reference_id: Mapped[UUID | None] = mapped_column(
        nullable=True, index=True, default=None
    )

    # chave para idempotência de chamadas externas
    idempotency_key: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        unique=True,
        default=None,
    )

    idempotency_payload_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )

    description: Mapped[str] = mapped_column(String(255), default="")

    # se esta transação reverte outra
    reversed_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ledger_transactions.id"),
        nullable=True,
        unique=True,
        default=None,
    )

    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction",
        init=False,
        default_factory=list,
        cascade="all, delete-orphan",
        foreign_keys="LedgerEntry.transaction_id",
    )

    reversed_transaction: Mapped["LedgerTransaction"] = relationship(
        remote_side="LedgerTransaction.id",
        init=False,
        default=None,
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
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    operation: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    resource_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(40))
    payload_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )


@table_registry.mapped_as_dataclass
class LedgerEntry(TimestampMixin):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "amount > 0", name="ck_ledger_entries_amount_positive"
        ),
        CheckConstraint(
            f"entry_type IN ({_enum_values(EntryType)})",
            name="ck_ledger_entries_entry_type_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("ledger_transactions.id"),
        index=True,
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id"),
        index=True,
    )

    entry_type: Mapped[str] = mapped_column(
        index=True,
    )

    # sempre positivo; o sentido está em entry_type
    amount: Mapped[int] = mapped_column()

    description: Mapped[str] = mapped_column(String(255), default="")

    # referência de negócio específica do lançamento
    reference_type: Mapped[str] = mapped_column(
        String(50), default="", index=True
    )
    reference_id: Mapped[UUID | None] = mapped_column(
        nullable=True, index=True, default=None
    )

    transaction: Mapped["LedgerTransaction"] = relationship(
        back_populates="entries",
        init=False,
    )

    account: Mapped["Account"] = relationship(
        back_populates="entries",
        init=False,
    )


@table_registry.mapped_as_dataclass
class BalanceHold(TimestampMixin):
    __tablename__ = "balance_holds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_balance_holds_amount_positive"),
        CheckConstraint(
            f"status IN ({_enum_values(HoldStatus)})",
            name="ck_balance_holds_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id"),
        index=True,
    )

    amount: Mapped[int] = mapped_column()

    status: Mapped[str] = mapped_column(
        default=HoldStatus.ACTIVE,
        index=True,
    )

    reason: Mapped[str] = mapped_column(String(100), default="")

    reference_type: Mapped[str] = mapped_column(
        String(50), default="", index=True
    )
    reference_id: Mapped[UUID | None] = mapped_column(
        nullable=True, index=True, default=None
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(120),
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

    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    account: Mapped["Account"] = relationship(
        back_populates="holds",
        init=False,
    )


@table_registry.mapped_as_dataclass
class LedgerAuditEvent(TimestampMixin):
    __tablename__ = "ledger_audit_events"

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[UUID] = mapped_column(index=True)
    service_name: Mapped[str] = mapped_column(String(80), default="")
    idempotency_key: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        default=None,
    )
    event_metadata: Mapped[dict] = mapped_column(
        JSON,
        default_factory=dict,
    )
