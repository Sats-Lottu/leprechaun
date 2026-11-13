from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.audit import AuditEvent, record_audit_event
from ledger.balances import (
    available_balance,
    consume_reserved,
    credit,
    debit_available,
)
from ledger.errors import ledger_error
from ledger.hold_helpers import get_hold_or_404, is_hold_expired
from ledger.models.enums import (
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
from ledger.payload_hash import payload_hash

INVOICE_EVENT_GRACE = timedelta(minutes=5)


class CheckoutSettlement(BaseModel):
    source_account_id: UUID
    destination_account_id: UUID
    internal_amount_msat: int = Field(ge=0)
    external_amount_msat: int = Field(ge=0)
    hold_id: UUID | None = None
    invoice_id: str | None = Field(default=None, max_length=160)
    expires_at: datetime

    @model_validator(mode='after')
    def validate_funding(self):
        if (
            not (self.internal_amount_msat or self.external_amount_msat)
            or bool(self.hold_id) != bool(self.internal_amount_msat)
            or self.source_account_id == self.destination_account_id
        ):
            raise ValueError('Invalid checkout funding')
        return self


def checkout_conflict(code: str, message: str):
    return ledger_error(status.HTTP_409_CONFLICT, code, message)


async def settle_checkout(
    checkout_id: UUID, request: CheckoutSettlement, session: AsyncSession
) -> LedgerTransaction:
    key = f'checkout:{checkout_id}:settle'
    hold = (
        await get_hold_or_404(request.hold_id, session, for_update=True)
        if request.hold_id
        else None
    )
    accounts = {
        account.id: account
        for account in await session.scalars(
            select(Account)
            .where(
                Account.id.in_((
                    request.source_account_id,
                    request.destination_account_id,
                ))
            )
            .order_by(Account.id)
            .with_for_update()
        )
    }
    existing = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == key
        )
    )
    request_hash = payload_hash(request)
    if existing is not None:
        if existing.idempotency_payload_hash != request_hash:
            raise checkout_conflict(
                'idempotency_payload_mismatch', 'Checkout funding changed'
            )
        return existing
    if (
        not {request.source_account_id, request.destination_account_id}
        <= accounts.keys()
    ):
        raise checkout_conflict(
            'account_not_found', 'Checkout account not found'
        )

    internal, external = await remaining_funding(checkout_id, request, session)
    expires_at = request.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if internal and not request.external_amount_msat and expires_at <= now:
        raise checkout_conflict('checkout_expired', 'Checkout has expired')

    source = accounts[request.source_account_id]
    released_hold = 0
    if external:
        await verify_invoice(
            checkout_id,
            request,
            session,
            expires_at,
            expiry_deadline=(
                now if internal == request.internal_amount_msat else None
            ),
        )

    if internal:
        released_hold = apply_checkout_hold(
            hold, request, source, checkout_id, now
        )

    total = internal + external
    if not total:
        # Both legacy legs (or the only leg) were posted before the upgrade.
        leg = 'external' if request.external_amount_msat else 'internal'
        return (
            await session.scalars(
                select(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == f'{key}:{leg}'
                )
            )
        ).one()

    available_debit = external + released_hold
    if available_balance(source) < available_debit:
        raise checkout_conflict(
            'checkout_funds_unavailable', 'Funds unavailable'
        )
    debit_available(source, available_debit)
    credit(accounts[request.destination_account_id], total)
    transaction = LedgerTransaction(
        status=TransactionStatus.POSTED,
        posted_at=now,
        reference_type='checkout_session',
        reference_id=checkout_id,
        idempotency_key=key,
        idempotency_payload_hash=request_hash,
        description='Checkout settlement',
    )
    session.add(transaction)
    await session.flush()
    for account_id, entry_type in (
        (source.id, EntryType.DEBIT),
        (request.destination_account_id, EntryType.CREDIT),
    ):
        session.add(
            LedgerEntry(
                transaction_id=transaction.id,
                account_id=account_id,
                entry_type=entry_type,
                amount=total,
                reference_type='checkout_session',
                reference_id=checkout_id,
                description='Checkout settlement',
            )
        )
    record_audit_event(
        session,
        AuditEvent(
            event_type='checkout_settled',
            resource_type='transaction',
            resource_id=transaction.id,
            idempotency_key=key,
            metadata={
                'checkout_id': str(checkout_id),
                'invoice_id': request.invoice_id,
            },
        ),
    )
    await session.commit()
    return transaction


def apply_checkout_hold(
    hold: BalanceHold | None,
    request: CheckoutSettlement,
    source: Account,
    checkout_id: UUID,
    now: datetime,
) -> int:
    amount = request.internal_amount_msat
    if hold is None or (
        hold.account_id,
        hold.reference_type,
        hold.reference_id,
        hold.amount,
    ) != (source.id, 'checkout_session', checkout_id, amount):
        raise checkout_conflict(
            'checkout_hold_unavailable', 'Invalid checkout hold'
        )
    if hold.status == HoldStatus.ACTIVE:
        if not request.external_amount_msat and is_hold_expired(hold, now):
            raise checkout_conflict('checkout_expired', 'Checkout has expired')
        consume_reserved(source, amount)
        hold.status = HoldStatus.CONSUMED
        hold.consumed_at = now
        return 0
    if hold.status == HoldStatus.EXPIRED and request.external_amount_msat:
        return amount
    raise checkout_conflict(
        'checkout_hold_unavailable', 'Invalid checkout hold'
    )


async def remaining_funding(
    checkout_id: UUID, request: CheckoutSettlement, session: AsyncSession
) -> tuple[int, int]:
    prefix = f'checkout:{checkout_id}:settle'
    posted = set(
        await session.scalars(
            select(LedgerTransaction.idempotency_key).where(
                LedgerTransaction.idempotency_key.in_((
                    f'{prefix}:internal',
                    f'{prefix}:external',
                )),
                LedgerTransaction.status == TransactionStatus.POSTED,
            )
        )
    )
    return (
        0 if f'{prefix}:internal' in posted else request.internal_amount_msat,
        0 if f'{prefix}:external' in posted else request.external_amount_msat,
    )


async def verify_invoice(
    checkout_id: UUID,
    request: CheckoutSettlement,
    session: AsyncSession,
    expires_at: datetime,
    *,
    expiry_deadline: datetime | None,
) -> None:
    if request.invoice_id:
        proof = await session.scalar(
            select(LedgerTransaction)
            .join(
                LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id
            )
            .where(
                LedgerTransaction.idempotency_key
                == f'payment.invoice.paid:{request.invoice_id}',
                LedgerTransaction.kind == TransactionKind.EXTERNAL_CREDIT,
                LedgerTransaction.external_origin == 'lightning',
                LedgerTransaction.status == TransactionStatus.POSTED,
                LedgerEntry.account_id == request.source_account_id,
                LedgerEntry.entry_type == EntryType.CREDIT,
                LedgerEntry.amount == request.external_amount_msat,
            )
        )
        if (
            proof is not None
            and proof.checkout_invoice_claim is None
            and (
                expiry_deadline is None
                or (proof.external_paid_at or proof.posted_at) <= expires_at
            )
        ):
            proof.checkout_invoice_claim = checkout_id
            return
    if (
        expiry_deadline is not None
        and expires_at + INVOICE_EVENT_GRACE <= expiry_deadline
    ):
        raise checkout_conflict('checkout_expired', 'Checkout has expired')
    raise checkout_conflict(
        'checkout_invoice_unconfirmed',
        'Checkout invoice has not been confirmed',
    )
