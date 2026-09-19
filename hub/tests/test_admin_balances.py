from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from hub.pages.admin_balances import (
    adjust_account_balance,
    reserve_account_balance,
)

ACCOUNT_ID = UUID('00000000-0000-0000-0000-000000000001')
TRANSACTION_ID = UUID('00000000-0000-0000-0000-000000000002')
HOLD_ID = UUID('00000000-0000-0000-0000-000000000003')
ADJUSTMENT_AMOUNT_MSAT = 5_000
HOLD_AMOUNT_MSAT = 7_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('operation', 'kind', 'entry_type'),
    [
        ('credit', 'external_credit', 'credit'),
        ('debit', 'external_debit', 'debit'),
    ],
)
async def test_adjust_account_balance_posts_external_transaction(
    operation: str,
    kind: str,
    entry_type: str,
) -> None:
    ledger = SimpleNamespace(
        create_transaction=AsyncMock(
            return_value=SimpleNamespace(
                transaction_id=TRANSACTION_ID,
                status='pending',
            )
        ),
        post_transaction=AsyncMock(
            return_value=SimpleNamespace(
                transaction_id=TRANSACTION_ID,
                status='posted',
            )
        ),
    )

    result = await adjust_account_balance(
        account_id=ACCOUNT_ID,
        amount_msat=ADJUSTMENT_AMOUNT_MSAT,
        operation=operation,
        reason='Support adjustment',
        actor_sub='admin-123',
        ledger=ledger,
    )

    payload = ledger.create_transaction.await_args.args[0]
    assert payload.kind == kind
    assert payload.external_origin == 'hub_admin'
    assert payload.entries[0].entry_type == entry_type
    assert payload.entries[0].amount_msat == ADJUSTMENT_AMOUNT_MSAT
    assert 'admin-123' in payload.description
    ledger.post_transaction.assert_awaited_once()
    assert result.resource_id == TRANSACTION_ID
    assert result.status == 'posted'


@pytest.mark.asyncio
async def test_reserve_account_balance_creates_hold() -> None:
    ledger = SimpleNamespace(
        create_hold=AsyncMock(
            return_value=SimpleNamespace(hold_id=HOLD_ID, status='active')
        )
    )

    result = await reserve_account_balance(
        account_id=ACCOUNT_ID,
        amount_msat=HOLD_AMOUNT_MSAT,
        reason='Fraud review',
        actor_sub='admin-123',
        expires_in_hours=24,
        ledger=ledger,
    )

    payload = ledger.create_hold.await_args.args[0]
    assert payload.account_id == ACCOUNT_ID
    assert payload.amount_msat == HOLD_AMOUNT_MSAT
    assert payload.expires_at is not None
    assert payload.reason == 'Admin hold by admin-123: Fraud review'
    assert result.resource_id == HOLD_ID
    assert result.status == 'active'
