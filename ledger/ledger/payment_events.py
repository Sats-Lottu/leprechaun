import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.audit import AuditEvent, record_audit_event
from ledger.balances import available_balance, credit, debit_available
from ledger.models.enums import AccountType, EntryType, TransactionStatus
from ledger.models.tables import Account, LedgerEntry, LedgerTransaction
from ledger.observability import PAYMENT_EVENTS, log_event
from ledger.settings import get_settings

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


def _event_idempotency_key(data: PaymentInvoicePaidData) -> str:
    return f'payment.invoice.paid:{data.invoice_id}'


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def _get_account_or_none(
    session: AsyncSession,
    account_id: UUID,
) -> Account | None:
    return await session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.is_active.is_(True),
        )
    )


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

    settings = get_settings()
    if settings.LIGHTNING_SETTLEMENT_ACCOUNT_ID is None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='missing_settlement_account',
        ).inc()
        raise RuntimeError('LIGHTNING_SETTLEMENT_ACCOUNT_ID is required')

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

    settlement_account = await _get_account_or_none(
        session,
        settings.LIGHTNING_SETTLEMENT_ACCOUNT_ID,
    )
    if settlement_account is None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='settlement_account_not_found',
        ).inc()
        raise RuntimeError('Lightning settlement account not found')

    user_account = await _get_user_account_or_none(session, data.user_id)
    if user_account is None:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='user_account_not_found',
        ).inc()
        raise RuntimeError('User account not found')

    if available_balance(settlement_account) < data.amount_msat:
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='insufficient_settlement_balance',
        ).inc()
        raise RuntimeError('Insufficient lightning settlement balance')

    transaction = LedgerTransaction(
        status=TransactionStatus.POSTED,
        reference_type='lightning_invoice',
        reference_id=_uuid_or_none(data.invoice_id),
        idempotency_key=idempotency_key,
        description=f'Lightning invoice paid {data.invoice_id}',
        posted_at=datetime.now(UTC),
    )
    session.add(transaction)
    await session.flush()

    for account, entry_type, description in (
        (
            settlement_account,
            EntryType.DEBIT,
            'Lightning settlement debit',
        ),
        (user_account, EntryType.CREDIT, 'User lightning deposit credit'),
    ):
        session.add(
            LedgerEntry(
                transaction_id=transaction.id,
                account_id=account.id,
                entry_type=entry_type,
                amount=data.amount_msat,
                description=description,
                reference_type='lightning_invoice',
                reference_id=_uuid_or_none(data.invoice_id),
            )
        )

    debit_available(settlement_account, data.amount_msat)
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


async def process_payment_event(
    raw_env: dict,
    session: AsyncSession,
) -> LedgerTransaction | None:
    env = PaymentEnvelope(**raw_env)
    if env.meta.type != 'payment.invoice.paid':
        PAYMENT_EVENTS.labels(
            event_type=env.meta.type,
            result='ignored',
        ).inc()
        return None

    return await apply_payment_invoice_paid(env, session)
