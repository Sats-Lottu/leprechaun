from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from ledger.checkout import CheckoutSettlement, settle_checkout
from ledger.models.enums import AccountType, HoldStatus, TransactionStatus
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerEntry,
    LedgerTransaction,
)
from ledger.payment_events import process_payment_event

pytestmark = pytest.mark.asyncio
INTERNAL = 4_000
EXTERNAL = 6_000
TOTAL = INTERNAL + EXTERNAL


@pytest.fixture
def checkout_id():
    return uuid4()


@pytest_asyncio.fixture
async def funding(session, checkout_id):
    source = Account(
        account_type=AccountType.USER,
        owner_id=uuid4(),
        balance=INTERNAL,
        reserved_balance=INTERNAL,
    )
    destination = Account(account_type=AccountType.USER, owner_id=uuid4())
    session.add_all([source, destination])
    await session.flush()
    hold = BalanceHold(
        account_id=source.id,
        amount=INTERNAL,
        reference_type='checkout_session',
        reference_id=checkout_id,
    )
    session.add(hold)
    await session.commit()
    request = CheckoutSettlement(
        source_account_id=source.id,
        destination_account_id=destination.id,
        internal_amount_msat=INTERNAL,
        external_amount_msat=EXTERNAL,
        invoice_id='expected-invoice',
        hold_id=hold.id,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return source, destination, hold, request


async def confirm_invoice(session, owner_id, invoice_id):
    return await process_payment_event(
        {
            'meta': {'type': 'payment.invoice.paid'},
            'data': {
                'user_id': str(owner_id),
                'invoice_id': invoice_id,
                'amount_msat': EXTERNAL,
                'paid_at': datetime.now(UTC).isoformat(),
            },
        },
        session,
    )


async def test_mixed_checkout_requires_its_invoice_and_settles_once(
    session,
    checkout_id,
    funding,
):
    source, destination, hold, request = funding
    await confirm_invoice(session, source.owner_id, 'unrelated-invoice')
    with pytest.raises(HTTPException, match='checkout_invoice_unconfirmed'):
        await settle_checkout(checkout_id, request, session)
    assert hold.status == HoldStatus.ACTIVE
    assert destination.balance == 0

    proof = await confirm_invoice(session, source.owner_id, request.invoice_id)
    first = await settle_checkout(checkout_id, request, session)
    replay = await settle_checkout(checkout_id, request, session)

    assert first.id == replay.id
    assert proof.checkout_invoice_claim == checkout_id
    assert hold.status == HoldStatus.CONSUMED
    assert source.balance == EXTERNAL
    assert source.reserved_balance == 0
    assert destination.balance == TOTAL
    entries = (
        await session.scalars(
            select(LedgerEntry).where(LedgerEntry.transaction_id == first.id)
        )
    ).all()
    assert {entry.entry_type: entry.amount for entry in entries} == {
        'debit': TOTAL,
        'credit': TOTAL,
    }


async def test_an_invoice_can_fund_only_one_checkout(
    session, checkout_id, funding
):
    source, destination, _, request = funding
    await confirm_invoice(session, source.owner_id, request.invoice_id)
    await confirm_invoice(session, source.owner_id, 'unrelated-invoice')
    request = request.model_copy(
        update={'internal_amount_msat': 0, 'hold_id': None}
    )
    await settle_checkout(checkout_id, request, session)
    with pytest.raises(HTTPException, match='checkout_invoice_unconfirmed'):
        await settle_checkout(uuid4(), request, session)
    assert destination.balance == EXTERNAL


async def test_legacy_partial_checkout_only_posts_remaining_leg(
    session,
    checkout_id,
    funding,
):
    source, destination, hold, request = funding
    source.balance = 0
    source.reserved_balance = 0
    hold.status = HoldStatus.CONSUMED
    hold.consumed_at = datetime.now(UTC)
    destination.balance = INTERNAL
    session.add(
        LedgerTransaction(
            idempotency_key=f'checkout:{checkout_id}:settle:internal',
            status=TransactionStatus.POSTED,
            posted_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await confirm_invoice(session, source.owner_id, request.invoice_id)
    request.expires_at = datetime.now(UTC) - timedelta(days=1)

    await settle_checkout(checkout_id, request, session)
    assert destination.balance == TOTAL
    assert source.balance == 0
