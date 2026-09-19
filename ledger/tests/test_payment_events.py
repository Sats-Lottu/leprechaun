from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

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
from ledger.payment_events import process_payment_event

pytestmark = pytest.mark.asyncio
DEPOSIT_AMOUNT_MSAT = 21_000
EXPECTED_ENTRY_COUNT = 1


async def test_payment_invoice_paid_creates_external_user_credit(session):
    user_id = uuid4()
    user = Account(account_type=AccountType.USER, owner_id=user_id)
    session.add(user)
    await session.commit()

    transaction = await process_payment_event(
        {
            'meta': {
                'type': 'payment.invoice.paid',
                'event_id': 'event-1',
                'correlation_id': 'corr-1',
            },
            'data': {
                'user_id': str(user_id),
                'invoice_id': 'lnbits-checking-id',
                'payment_hash': 'hash-1',
                'amount_msat': DEPOSIT_AMOUNT_MSAT,
                'paid_at': datetime.now(UTC).isoformat(),
            },
        },
        session,
    )

    assert transaction is not None
    assert transaction.status == TransactionStatus.POSTED
    assert transaction.kind == TransactionKind.EXTERNAL_CREDIT
    assert transaction.external_origin == 'lightning'
    assert transaction.idempotency_key == (
        'payment.invoice.paid:lnbits-checking-id'
    )
    assert user.balance == DEPOSIT_AMOUNT_MSAT
    entries = (
        await session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.transaction_id == transaction.id)
            .order_by(LedgerEntry.entry_type)
        )
    ).all()
    assert len(entries) == EXPECTED_ENTRY_COUNT
    assert entries[0].entry_type == EntryType.CREDIT
    assert entries[0].account_id == user.id


async def test_payment_invoice_paid_is_idempotent(session):
    user_id = uuid4()
    user = Account(account_type=AccountType.USER, owner_id=user_id)
    session.add(user)
    await session.commit()
    raw_event = {
        'meta': {'type': 'payment.invoice.paid', 'event_id': 'event-1'},
        'data': {
            'user_id': str(user_id),
            'invoice_id': 'lnbits-checking-id',
            'payment_hash': 'hash-1',
            'amount_msat': DEPOSIT_AMOUNT_MSAT,
            'paid_at': datetime.now(UTC).isoformat(),
        },
    }

    first = await process_payment_event(raw_event, session)
    second = await process_payment_event(raw_event, session)

    transactions = (
        await session.scalars(select(LedgerTransaction))
    ).all()
    assert first is not None
    assert second is None
    assert len(transactions) == 1
    assert user.balance == DEPOSIT_AMOUNT_MSAT


async def test_payment_sent_consumes_withdrawal_hold(session):
    user_id = uuid4()
    payment_reference = uuid4()
    user = Account(
        account_type=AccountType.USER,
        owner_id=user_id,
        balance=DEPOSIT_AMOUNT_MSAT,
        reserved_balance=DEPOSIT_AMOUNT_MSAT,
    )
    hold = BalanceHold(
        account_id=user.id,
        amount=DEPOSIT_AMOUNT_MSAT,
        reference_type='wallet_withdrawal',
        reference_id=payment_reference,
    )
    session.add_all([user, hold])
    await session.commit()

    transaction = await process_payment_event(
        {
            'meta': {
                'type': 'payment.sent',
                'event_id': 'event-withdrawal',
                'correlation_id': 'corr-withdrawal',
            },
            'data': {
                'user_id': str(user_id),
                'payment_reference': str(payment_reference),
                'payment_hash': 'hash-out',
                'checking_id': 'check-out',
                'amount_msat': DEPOSIT_AMOUNT_MSAT,
                'paid_at': datetime.now(UTC).isoformat(),
            },
        },
        session,
    )

    assert transaction is not None
    assert transaction.kind == TransactionKind.EXTERNAL_DEBIT
    assert transaction.external_origin == 'lightning'
    assert transaction.status == TransactionStatus.POSTED
    assert user.balance == 0
    assert user.reserved_balance == 0
    assert hold.status == HoldStatus.CONSUMED
