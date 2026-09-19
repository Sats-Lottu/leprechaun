import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.audit import AuditEvent, record_audit_event
from ledger.balances import credit
from ledger.models.enums import (
    AccountType,
    EntryType,
    HoldStatus,
    TransactionKind,
    TransactionStatus,
)
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerEntry,
    LedgerTransaction,
)
from ledger.observability import PAYMENT_EVENTS, log_event
from ledger.routes.transactions import post_transaction_using_reserved_hold

logger = logging.getLogger(__name__)


class PaymentMeta(BaseModel):
    model_config = ConfigDict(extra='ignore')

    type: str
    event_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = None


class PaymentEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')

    meta: PaymentMeta
    data: dict


class PaymentInvoicePaidData(BaseModel):
    model_config = ConfigDict(extra='forbid')

    user_id: UUID
    invoice_id: str
    payment_hash: str = ''
    amount_msat: int = Field(gt=0)
    paid_at: datetime


class PaymentSentData(BaseModel):
    model_config = ConfigDict(extra='forbid')

    user_id: UUID | None = None
    payment_reference: UUID | None = None
    payment_hash: str = ''
    checking_id: str = ''
    amount_msat: int | None = Field(default=None, gt=0)
    paid_at: datetime


def _event_idempotency_key(data: PaymentInvoicePaidData) -> str:
    return f'payment.invoice.paid:{data.invoice_id}'


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def _get_user_account_or_none(
    session: AsyncSession,
    owner_id: UUID,
) -> Account | None:
    return await session.scalar(
        select(Account).where(
            Account.account_type == AccountType.USER,
            Account.owner_id == owner_id,
            Account.is_active.is_(True),
        )
    )


async def apply_payment_invoice_paid(
    env: PaymentEnvelope,
    session: AsyncSession,
) -> LedgerTransaction | None:
    try:
        data = PaymentInvoicePaidData(**env.data)
    except ValidationError:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='invalid_payload',
        ).inc()
        log_event(
            logger,
            logging.WARNING,
            'payment_event_invalid_payload',
            event_type=env.meta.type,
            correlation_id=env.meta.correlation_id,
        )
        raise

    idempotency_key = _event_idempotency_key(data)
    existing_transaction = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == idempotency_key
        )
    )
    if existing_transaction is not None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='duplicate',
        ).inc()
        log_event(
            logger,
            logging.INFO,
            'payment_event_duplicate',
            event_type=env.meta.type,
            invoice_id=data.invoice_id,
            transaction_id=existing_transaction.id,
        )
        return None

    user_account = await _get_user_account_or_none(session, data.user_id)
    if user_account is None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='user_account_not_found',
        ).inc()
        raise RuntimeError('User account not found')

    transaction = LedgerTransaction(
        status=TransactionStatus.POSTED,
        kind=TransactionKind.EXTERNAL_CREDIT,
        external_origin='lightning',
        reference_type='lightning_invoice',
        reference_id=_uuid_or_none(data.invoice_id),
        idempotency_key=idempotency_key,
        description=f'Lightning invoice paid {data.invoice_id}',
        posted_at=datetime.now(UTC),
    )
    session.add(transaction)
    await session.flush()

    session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=user_account.id,
            entry_type=EntryType.CREDIT,
            amount=data.amount_msat,
            description='External Lightning deposit credit',
            reference_type='lightning_invoice',
            reference_id=_uuid_or_none(data.invoice_id),
        )
    )
    credit(user_account, data.amount_msat)
    record_audit_event(
        session,
        AuditEvent(
            event_type='payment_invoice_paid_applied',
            resource_type='transaction',
            resource_id=transaction.id,
            idempotency_key=idempotency_key,
            metadata={
                'event_id': env.meta.event_id,
                'correlation_id': env.meta.correlation_id,
                'invoice_id': data.invoice_id,
                'payment_hash': data.payment_hash,
                'user_id': str(data.user_id),
                'amount_msat': data.amount_msat,
            },
        ),
    )
    await session.commit()
    PAYMENT_EVENTS.labels(event_type=env.meta.type, result='processed').inc()
    log_event(
        logger,
        logging.INFO,
        'payment_invoice_paid_applied',
        invoice_id=data.invoice_id,
        payment_hash=data.payment_hash,
        user_id=data.user_id,
        amount_msat=data.amount_msat,
        transaction_id=transaction.id,
    )
    return transaction


async def apply_payment_sent(
    env: PaymentEnvelope,
    session: AsyncSession,
) -> LedgerTransaction | None:
    data = PaymentSentData(**env.data)
    if data.payment_reference is None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type, result='ignored'
        ).inc()
        return None
    if data.user_id is None or data.amount_msat is None:
        raise RuntimeError(
            'Referenced external payment requires user_id and amount_msat'
        )

    idempotency_key = f'payment.sent:{data.payment_reference}'
    existing_transaction = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == idempotency_key
        )
    )
    if existing_transaction is not None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type, result='duplicate'
        ).inc()
        return None

    user_account = await _get_user_account_or_none(session, data.user_id)
    if user_account is None:
        raise RuntimeError('User account not found')

    hold = await session.scalar(
        select(BalanceHold)
        .where(
            BalanceHold.account_id == user_account.id,
            BalanceHold.reference_type == 'wallet_withdrawal',
            BalanceHold.reference_id == data.payment_reference,
            BalanceHold.status == HoldStatus.ACTIVE,
        )
        .with_for_update()
    )
    if hold is None:
        raise RuntimeError('Active wallet withdrawal hold not found')
    if hold.amount != data.amount_msat:
        raise RuntimeError('Wallet withdrawal amount does not match hold')

    transaction = LedgerTransaction(
        kind=TransactionKind.EXTERNAL_DEBIT,
        external_origin='lightning',
        reference_type='lightning_payment',
        reference_id=data.payment_reference,
        idempotency_key=idempotency_key,
        description='External Lightning withdrawal',
    )
    session.add(transaction)
    await session.flush()
    session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=user_account.id,
            entry_type=EntryType.DEBIT,
            amount=data.amount_msat,
            description='External Lightning withdrawal debit',
            reference_type='lightning_payment',
            reference_id=data.payment_reference,
        )
    )
    await session.flush()
    await session.refresh(transaction, attribute_names=['entries'])
    await post_transaction_using_reserved_hold(transaction.id, hold, session)
    hold.status = HoldStatus.CONSUMED
    hold.consumed_at = datetime.now(UTC)
    record_audit_event(
        session,
        AuditEvent(
            event_type='payment_sent_applied',
            resource_type='transaction',
            resource_id=transaction.id,
            idempotency_key=idempotency_key,
            metadata={
                'event_id': env.meta.event_id,
                'correlation_id': env.meta.correlation_id,
                'payment_reference': str(data.payment_reference),
                'payment_hash': data.payment_hash,
                'checking_id': data.checking_id,
                'user_id': str(data.user_id),
                'amount_msat': data.amount_msat,
                'hold_id': str(hold.id),
            },
        ),
    )
    await session.commit()
    PAYMENT_EVENTS.labels(event_type=env.meta.type, result='processed').inc()
    return transaction


async def process_payment_event(
    raw_env: dict,
    session: AsyncSession,
) -> LedgerTransaction | None:
    env = PaymentEnvelope(**raw_env)
    if env.meta.type == 'payment.invoice.paid':
        return await apply_payment_invoice_paid(env, session)
    if env.meta.type == 'payment.sent':
        return await apply_payment_sent(env, session)
    PAYMENT_EVENTS.labels(
        event_type=env.meta.type,
        result='ignored',
    ).inc()
    return None
