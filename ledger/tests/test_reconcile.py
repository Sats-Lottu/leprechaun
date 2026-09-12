from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from ledger.idempotency import (
    OperationIdempotencyLookup,
    get_operation_idempotency,
)
from ledger.models.enums import EntryType, HoldStatus, TransactionStatus
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerEntry,
    LedgerTransaction,
    OperationIdempotency,
)
from ledger.reconcile import (
    ReconciliationReport,
    check_account_balances,
    parse_args,
    reconcile_session,
    run_reconciliation,
)


@pytest.mark.asyncio
async def test_reconcile_session_passes_clean_ledger(session, user_account):
    report = await reconcile_session(session)

    assert report.ok
    assert report.issues == []


@pytest.mark.asyncio
async def test_reconcile_session_detects_reserved_balance_mismatch(session):
    account = Account(balance=100, reserved_balance=40)
    session.add(account)
    await session.commit()

    report = await reconcile_session(session)

    assert not report.ok
    assert [issue.code for issue in report.issues] == [
        "reserved_balance_mismatch"
    ]


@pytest.mark.asyncio
async def test_reconcile_session_detects_unbalanced_posted_transaction(
    session, user_account
):
    transaction = LedgerTransaction(status=TransactionStatus.POSTED)
    session.add(transaction)
    await session.flush()
    session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=user_account.id,
            entry_type=EntryType.DEBIT,
            amount=100,
        )
    )
    await session.commit()

    report = await reconcile_session(session)

    assert "transaction_not_balanced" in [
        issue.code for issue in report.issues
    ]
    assert "posted_transaction_missing_posted_at" in [
        issue.code for issue in report.issues
    ]


@pytest.mark.asyncio
async def test_reconcile_session_detects_closed_hold_missing_timestamp(
    session, user_account
):
    hold = BalanceHold(
        account_id=user_account.id,
        amount=10,
        status=HoldStatus.RELEASED,
        reference_id=uuid4(),
    )
    session.add(hold)
    await session.commit()

    report = await reconcile_session(session)

    assert "closed_hold_missing_timestamp" in [
        issue.code for issue in report.issues
    ]


@pytest.mark.asyncio
async def test_reconcile_account_balance_checks_report_direct_violations():
    account = SimpleNamespace(
        id=uuid4(),
        balance=-1,
        reserved_balance=2,
    )

    async def scalars(_query):
        return [account]

    report = ReconciliationReport()

    await check_account_balances(SimpleNamespace(scalars=scalars), report)

    assert [issue.code for issue in report.issues] == [
        "negative_balance",
        "reserved_balance_exceeds_balance",
    ]


@pytest.mark.asyncio
async def test_reconcile_account_balance_checks_report_negative_reserved():
    account = SimpleNamespace(
        id=uuid4(),
        balance=0,
        reserved_balance=-1,
    )

    async def scalars(_query):
        return [account]

    report = ReconciliationReport()

    await check_account_balances(SimpleNamespace(scalars=scalars), report)

    assert [issue.code for issue in report.issues] == [
        "negative_reserved_balance"
    ]


@pytest.mark.asyncio
async def test_run_reconciliation_uses_database_session(monkeypatch, session):
    monkeypatch.setattr("ledger.reconcile.engine", session.bind)

    report = await run_reconciliation()

    assert report.ok


def test_reconcile_parse_args():
    args = parse_args([])

    assert vars(args) == {}


@pytest.mark.asyncio
async def test_get_operation_idempotency_rejects_payload_mismatch(session):
    operation = OperationIdempotency(
        operation="hold.release",
        resource_id=uuid4(),
        idempotency_key="release-1",
        status=HoldStatus.RELEASED,
        payload_hash="first",
    )
    session.add(operation)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await get_operation_idempotency(
            session,
            OperationIdempotencyLookup(
                operation="hold.release",
                resource_id=operation.resource_id,
                idempotency_key="release-1",
                payload_hash="second",
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert exc.value.detail == {
        "code": "idempotency_payload_mismatch",
        "message": "Idempotency key was reused with a different payload",
    }
