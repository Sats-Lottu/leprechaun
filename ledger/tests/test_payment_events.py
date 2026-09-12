from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from ledger.models.enums import AccountType, EntryType, TransactionStatus
from ledger.models.tables import Account, LedgerEntry, LedgerTransaction
from ledger.payment_events import process_payment_event
from ledger.settings import get_settings

pytestmark = pytest.mark.asyncio
INITIAL_SETTLEMENT_BALANCE = 100_000
DEPOSIT_AMOUNT_MSAT = 21_000
SETTLEMENT_BALANCE_AFTER_DEPOSIT = 79_000
EXPECTED_ENTRY_COUNT = 2


async def test_payment_invoice_paid_credits_user_from_settlement_account(
    monkeypatch,
    session,
):
    user_id = uuid4()
    settlement = Account(
        account_type=AccountType.ESCROW,
        name='Lightning settlement',
        balance=INITIAL_SETTLEMENT_BALANCE,
    )
    user = Account(account_type=AccountType.USER, owner_id=user_id)
    session.add_all([settlement, user])
    await session.commit()
    monkeypatch.setenv(
        'LIGHTNING_SETTLEMENT_ACCOUNT_ID',
        str(settlement.id),
    )
    get_settings.cache_clear()

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
    assert transaction.idempotency_key == (
        'payment.invoice.paid:lnbits-checking-id'
    )
    assert settlement.balance == SETTLEMENT_BALANCE_AFTER_DEPOSIT
    assert user.balance == DEPOSIT_AMOUNT_MSAT
    entries = (
        await session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.transaction_id == transaction.id)
            .order_by(LedgerEntry.entry_type)
        )
    ).all()
    assert len(entries) == EXPECTED_ENTRY_COUNT
    assert {entry.entry_type for entry in entries} == {
        EntryType.CREDIT,
        EntryType.DEBIT,
    }


async def test_payment_invoice_paid_is_idempotent(monkeypatch, session):
    user_id = uuid4()
    settlement = Account(
        account_type=AccountType.ESCROW,
        name='Lightning settlement',
        balance=INITIAL_SETTLEMENT_BALANCE,
    )
    user = Account(account_type=AccountType.USER, owner_id=user_id)
    session.add_all([settlement, user])
    await session.commit()
    monkeypatch.setenv(
        'LIGHTNING_SETTLEMENT_ACCOUNT_ID',
        str(settlement.id),
    )
    get_settings.cache_clear()
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
    assert settlement.balance == SETTLEMENT_BALANCE_AFTER_DEPOSIT
    assert user.balance == DEPOSIT_AMOUNT_MSAT
