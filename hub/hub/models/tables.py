from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, registry, relationship

from hub.models.enums import AdminRoleStatus, CheckoutSessionStatus

table_registry = registry()


def _enum_values(enum_cls: type) -> str:
    return ', '.join(f"'{item.value}'" for item in enum_cls)


@dataclass
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
class AdminRoleAssignment(TimestampMixin):
    __tablename__ = 'admin_role_assignments'
    __table_args__ = (
        UniqueConstraint(
            'user_sub',
            'role',
            name='uq_admin_role_assignments_user_sub_role',
        ),
        CheckConstraint(
            f'status IN ({_enum_values(AdminRoleStatus)})',
            name='ck_admin_role_assignments_status_valid',
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    user_sub: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(
        String(40),
        default=AdminRoleStatus.ACTIVE,
        index=True,
    )
    granted_by: Mapped[str] = mapped_column(String(255), default='')
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )


@table_registry.mapped_as_dataclass
class UserLedgerAccount(TimestampMixin):
    __tablename__ = 'user_ledger_accounts'

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    user_sub: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )
    ledger_account_id: Mapped[UUID] = mapped_column(unique=True, index=True)

    checkout_sessions: Mapped[list['CheckoutSession']] = relationship(
        back_populates='ledger_account',
        init=False,
        default_factory=list,
    )


@table_registry.mapped_as_dataclass
class ConnectedApplication(TimestampMixin):
    __tablename__ = 'connected_applications'

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    destination_account_id: Mapped[UUID] = mapped_column()
    website_url: Mapped[str] = mapped_column(String(2048))
    key_hash: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)


@table_registry.mapped_as_dataclass
class ApplicationAudit(TimestampMixin):
    __tablename__ = 'application_audit'

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey('connected_applications.id'), index=True
    )
    actor_sub: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(40))


@table_registry.mapped_as_dataclass
class CheckoutSession(TimestampMixin):
    __tablename__ = 'checkout_sessions'
    __table_args__ = (
        UniqueConstraint(
            'game_id',
            'order_id',
            name='uq_checkout_sessions_game_order',
        ),
        CheckConstraint(
            'amount_msat > 0',
            name='ck_checkout_sessions_amount_msat_positive',
        ),
        CheckConstraint(
            f'status IN ({_enum_values(CheckoutSessionStatus)})',
            name='ck_checkout_sessions_status_valid',
        ),
    )

    id: Mapped[UUID] = mapped_column(
        init=False, primary_key=True, default_factory=uuid4
    )
    game_id: Mapped[str] = mapped_column(String(120), index=True)
    order_id: Mapped[str] = mapped_column(String(160), index=True)
    amount_msat: Mapped[int] = mapped_column()
    description: Mapped[str] = mapped_column(String(280))
    return_url: Mapped[str] = mapped_column(String(2048))
    cancel_url: Mapped[str] = mapped_column(String(2048))
    checkout_url: Mapped[str] = mapped_column(String(2048))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default=CheckoutSessionStatus.CREATED,
        index=True,
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey('connected_applications.id'), default=None, index=True
    )
    internal_amount_msat: Mapped[int] = mapped_column(default=0)
    external_amount_msat: Mapped[int] = mapped_column(default=0)
    user_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        default=None,
    )
    destination_account_id: Mapped[UUID | None] = mapped_column(
        nullable=True,
        index=True,
        default=None,
    )
    ledger_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey('user_ledger_accounts.ledger_account_id'),
        nullable=True,
        index=True,
        default=None,
    )
    ledger_hold_id: Mapped[UUID | None] = mapped_column(
        nullable=True,
        index=True,
        default=None,
    )
    invoice_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
        default=None,
    )
    payment_request: Mapped[str] = mapped_column(String(4096), default='')
    invoice_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        default=None,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    ledger_account: Mapped[UserLedgerAccount | None] = relationship(
        back_populates='checkout_sessions',
        init=False,
        default=None,
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
