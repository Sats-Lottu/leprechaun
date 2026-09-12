import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger import hold_expiration_worker
from ledger.hold_expiration import expire_due_holds_in_session
from ledger.hold_helpers import is_hold_expired
from ledger.models.enums import EntryType, HoldStatus, TransactionStatus
from ledger.models.tables import (
    BalanceHold,
    LedgerAuditEvent,
    LedgerTransaction,
)
from ledger.routes.holds import (
    consume_hold,
    create_hold,
    expire_due_holds,
    expire_hold,
    get_hold,
    list_account_holds,
    release_hold,
)
from ledger.routes.transactions import create_transaction
from ledger.schemas import (
    HoldCreate,
    HoldOperation,
    TransactionCreate,
    TransactionEntryCreate,
)


def assert_error(exc: HTTPException, code: str, message: str) -> None:
    assert exc.detail == {"code": code, "message": message}


async def create_consume_transaction(session, hold, credit_account):
    return await create_transaction(
        session,
        TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=hold.account_id,
                    entry_type=EntryType.DEBIT,
                    amount=hold.amount,
                ),
                TransactionEntryCreate(
                    account_id=credit_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=hold.amount,
                ),
            ],
        ),
    )


def test_create_hold(client, user_account):
    user_account.balance = 100

    response = client.post(
        "/holds",
        json={
            "account_id": str(user_account.id),
            "amount": 40,
            "reason": "checkout",
            "reference_type": "payment_order",
            "reference_id": str(uuid4()),
            "idempotency_key": "hold-1",
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json() == {
        "hold_id": response.json()["hold_id"],
        "status": "active",
    }


@pytest.mark.asyncio
async def test_create_hold_function_success(session, user_account):
    user_account.balance = 100
    reference_id = uuid4()
    await session.commit()

    result = await create_hold(
        session,
        HoldCreate(
            account_id=user_account.id,
            amount=40,
            reason="checkout",
            reference_type="payment_order",
            reference_id=reference_id,
            idempotency_key="hold-1",
        ),
    )
    hold = await session.scalar(
        select(BalanceHold).where(BalanceHold.id == result.hold_id)
    )

    assert result.status == HoldStatus.ACTIVE
    assert hold is not None
    assert hold.account_id == user_account.id
    assert hold.amount == 40  # noqa: PLR2004
    assert hold.reason == "checkout"
    assert hold.reference_type == "payment_order"
    assert hold.reference_id == reference_id
    assert hold.idempotency_key == "hold-1"
    assert hold.idempotency_payload_hash is not None
    assert user_account.reserved_balance == 40  # noqa: PLR2004
    assert user_account.balance == 100  # noqa: PLR2004
    audit_event = await session.scalar(
        select(LedgerAuditEvent).where(
            LedgerAuditEvent.resource_id == hold.id,
            LedgerAuditEvent.event_type == "hold_created",
        )
    )
    assert audit_event is not None
    assert audit_event.event_metadata["amount"] == 40  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_hold_function_reuses_idempotency_key(
    session, user_account
):
    user_account.balance = 100
    await session.commit()
    payload = HoldCreate(
        account_id=user_account.id,
        amount=40,
        idempotency_key="hold-1",
    )

    first_result = await create_hold(session, payload)
    second_result = await create_hold(session, payload)

    assert second_result == first_result
    assert user_account.reserved_balance == 40  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_hold_function_rejects_idempotency_payload_mismatch(
    session, user_account
):
    user_account.balance = 100
    await session.commit()
    payload = HoldCreate(
        account_id=user_account.id,
        amount=40,
        idempotency_key="hold-1",
    )
    await create_hold(session, payload)

    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(
                account_id=user_account.id,
                amount=50,
                idempotency_key="hold-1",
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "idempotency_payload_mismatch",
        "Idempotency key was reused with a different payload",
    )


@pytest.mark.asyncio
async def test_create_hold_function_rejects_zero_amount(session, user_account):
    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(account_id=user_account.id, amount=0),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        "invalid_hold_amount",
        "Hold amount must be greater than zero",
    )


@pytest.mark.asyncio
async def test_create_hold_function_rejects_missing_account(session):
    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(account_id=uuid4(), amount=40),
        )

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert_error(exc.value, "account_not_found", "Account not found")


@pytest.mark.asyncio
async def test_create_hold_function_rejects_insufficient_available_balance(
    session, user_account
):
    user_account.balance = 100
    user_account.reserved_balance = 80
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(account_id=user_account.id, amount=40),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        "insufficient_available_balance",
        "Insufficient available balance",
    )


@pytest.mark.asyncio
async def test_create_hold_concurrent_requests_cannot_overspend(
    engine, user_account_with_balance
):
    async def create_competing_hold(idempotency_key: str):
        async with AsyncSession(
            engine, expire_on_commit=False
        ) as local_session:
            try:
                return await create_hold(
                    local_session,
                    HoldCreate(
                        account_id=user_account_with_balance.id,
                        amount=80,
                        idempotency_key=idempotency_key,
                    ),
                )
            except HTTPException as exc:
                return exc

    results = await asyncio.gather(
        create_competing_hold("hold-race-1"),
        create_competing_hold("hold-race-2"),
    )

    created = [
        result for result in results if not isinstance(result, Exception)
    ]
    rejected = [
        result for result in results if isinstance(result, HTTPException)
    ]

    assert len(created) == 1
    assert len(rejected) == 1
    assert rejected[0].status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        rejected[0],
        "insufficient_available_balance",
        "Insufficient available balance",
    )


@pytest.mark.asyncio
async def test_create_hold_function_handles_idempotency_commit_conflict(
    monkeypatch, session, user_account
):
    user_account.balance = 100
    await session.commit()

    async def fake_commit():
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(session, "commit", fake_commit)

    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(
                account_id=user_account.id,
                amount=40,
                idempotency_key="hold-1",
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "hold_idempotency_conflict",
        "Hold idempotency key already exists",
    )


@pytest.mark.asyncio
async def test_create_hold_function_returns_existing_after_commit_conflict(
    monkeypatch, session, account_with_existing_hold
):
    account, existing_hold = account_with_existing_hold
    existing_hold_result = SimpleNamespace(
        id=existing_hold.id,
        status=existing_hold.status,
    )
    scalar_results = iter([None, account, existing_hold_result])

    async def fake_scalar(query):
        return next(scalar_results)

    async def fake_commit():
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(session, "scalar", fake_scalar)
    monkeypatch.setattr(session, "commit", fake_commit)

    result = await create_hold(
        session,
        HoldCreate(
            account_id=account.id,
            amount=40,
            idempotency_key=existing_hold.idempotency_key,
        ),
    )

    assert result.hold_id == existing_hold_result.id
    assert result.status == HoldStatus.ACTIVE


@pytest.mark.asyncio
async def test_create_hold_function_rejects_commit_conflict_payload_mismatch(
    monkeypatch, session, account_with_existing_hold
):
    account, existing_hold = account_with_existing_hold
    existing_hold_result = SimpleNamespace(
        id=existing_hold.id,
        status=existing_hold.status,
        idempotency_payload_hash="different",
    )
    scalar_results = iter([None, account, existing_hold_result])

    async def fake_scalar(query):
        return next(scalar_results)

    async def fake_commit():
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(session, "scalar", fake_scalar)
    monkeypatch.setattr(session, "commit", fake_commit)

    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(
                account_id=account.id,
                amount=40,
                idempotency_key=existing_hold.idempotency_key,
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "idempotency_payload_mismatch",
        "Idempotency key was reused with a different payload",
    )


@pytest.mark.asyncio
async def test_create_hold_function_rejects_commit_conflict_without_key(
    monkeypatch, session, user_account_with_balance
):
    async def fake_commit():
        raise IntegrityError("insert", {}, Exception("constraint"))

    monkeypatch.setattr(session, "commit", fake_commit)

    with pytest.raises(HTTPException) as exc:
        await create_hold(
            session,
            HoldCreate(
                account_id=user_account_with_balance.id,
                amount=40,
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "hold_idempotency_conflict",
        "Hold idempotency key already exists",
    )


@pytest.mark.asyncio
async def test_get_hold_function_success(session, account_with_existing_hold):
    account, hold = account_with_existing_hold

    result = await get_hold(hold.id, session)

    assert result.hold_id == hold.id
    assert result.account_id == account.id
    assert result.amount == 40  # noqa: PLR2004
    assert result.status == HoldStatus.ACTIVE


@pytest.mark.asyncio
async def test_get_hold_function_not_found(session):
    with pytest.raises(HTTPException) as exc:
        await get_hold(uuid4(), session)

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert_error(exc.value, "hold_not_found", "Hold not found")


@pytest.mark.asyncio
async def test_list_account_holds_function_success(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold

    result = await list_account_holds(account.id, session)

    assert result.account_id == account.id
    assert [account_hold.hold_id for account_hold in result.holds] == [hold.id]


@pytest.mark.asyncio
async def test_list_account_holds_function_rejects_missing_account(session):
    with pytest.raises(HTTPException) as exc:
        await list_account_holds(uuid4(), session)

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert_error(exc.value, "account_not_found", "Account not found")


@pytest.mark.asyncio
async def test_consume_hold_function_success(
    session, account_with_existing_hold, other_user_account
):
    account, hold = account_with_existing_hold
    transaction = await create_consume_transaction(
        session, hold, other_user_account
    )

    with freeze_time("2024-01-02T12:00:00Z"):
        result = await consume_hold(
            hold.id,
            session,
            HoldOperation(transaction_id=transaction.transaction_id),
        )
    posted_transaction = await session.get(
        LedgerTransaction, transaction.transaction_id
    )

    assert result.hold_id == hold.id
    assert result.status == HoldStatus.CONSUMED
    assert hold.consumed_at == datetime(2024, 1, 2, 12, tzinfo=UTC)
    assert account.balance == 60  # noqa: PLR2004
    assert account.reserved_balance == 0
    assert other_user_account.balance == 40  # noqa: PLR2004
    assert posted_transaction.status == TransactionStatus.POSTED
    audit_event = await session.scalar(
        select(LedgerAuditEvent).where(
            LedgerAuditEvent.resource_id == hold.id,
            LedgerAuditEvent.event_type == "hold_consumed",
        )
    )
    assert audit_event is not None


@pytest.mark.asyncio
async def test_consume_hold_function_reuses_idempotency_key(
    session, account_with_existing_hold, other_user_account
):
    account, hold = account_with_existing_hold
    transaction = await create_consume_transaction(
        session, hold, other_user_account
    )
    payload = HoldOperation(
        idempotency_key="consume-hold-1",
        transaction_id=transaction.transaction_id,
    )

    first_result = await consume_hold(hold.id, session, payload)
    second_result = await consume_hold(hold.id, session, payload)

    assert second_result == first_result
    assert account.balance == 60  # noqa: PLR2004
    assert account.reserved_balance == 0
    assert other_user_account.balance == 40  # noqa: PLR2004


@pytest.mark.asyncio
async def test_consume_hold_function_requires_transaction(
    session, account_with_existing_hold
):
    _, hold = account_with_existing_hold

    with pytest.raises(HTTPException) as exc:
        await consume_hold(hold.id, session)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        "hold_consume_transaction_required",
        "A transaction_id is required to consume a hold",
    )


@pytest.mark.asyncio
async def test_consume_hold_function_rejects_transaction_debit_mismatch(
    session, account_with_existing_hold, other_user_account
):
    _, hold = account_with_existing_hold
    transaction = await create_transaction(
        session,
        TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=hold.account_id,
                    entry_type=EntryType.DEBIT,
                    amount=hold.amount - 1,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=hold.amount - 1,
                ),
            ],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await consume_hold(
            hold.id,
            session,
            HoldOperation(transaction_id=transaction.transaction_id),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        "hold_transaction_debit_mismatch",
        "Transaction must debit the hold account for the hold amount",
    )


@pytest.mark.asyncio
async def test_consume_hold_function_rejects_non_pending_transaction(
    session, account_with_existing_hold, other_user_account
):
    _, hold = account_with_existing_hold
    transaction_result = await create_consume_transaction(
        session, hold, other_user_account
    )
    transaction = await session.get(
        LedgerTransaction, transaction_result.transaction_id
    )
    transaction.status = TransactionStatus.POSTED
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await consume_hold(
            hold.id,
            session,
            HoldOperation(transaction_id=transaction.id),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "transaction_not_pending",
        "Only pending transactions can be posted",
    )


@pytest.mark.asyncio
async def test_consume_hold_function_posts_extra_debit_from_available_balance(
    session,
    account_with_existing_hold,
    user_account_with_balance,
    other_user_account,
):
    account, hold = account_with_existing_hold
    transaction = await create_transaction(
        session,
        TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=hold.account_id,
                    entry_type=EntryType.DEBIT,
                    amount=hold.amount,
                ),
                TransactionEntryCreate(
                    account_id=user_account_with_balance.id,
                    entry_type=EntryType.DEBIT,
                    amount=10,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=hold.amount + 10,
                ),
            ],
        ),
    )

    result = await consume_hold(
        hold.id,
        session,
        HoldOperation(transaction_id=transaction.transaction_id),
    )
    posted_transaction = await session.get(
        LedgerTransaction, transaction.transaction_id
    )

    assert result.status == HoldStatus.CONSUMED
    assert account.balance == 60  # noqa: PLR2004
    assert account.reserved_balance == 0
    assert user_account_with_balance.balance == 90  # noqa: PLR2004
    assert other_user_account.balance == 50  # noqa: PLR2004
    assert posted_transaction.status == TransactionStatus.POSTED


@pytest.mark.asyncio
async def test_consume_hold_function_rejects_extra_debit_without_balance(
    session, account_with_existing_hold, user_account, other_user_account
):
    _, hold = account_with_existing_hold
    transaction = await create_transaction(
        session,
        TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=hold.account_id,
                    entry_type=EntryType.DEBIT,
                    amount=hold.amount,
                ),
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=10,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=hold.amount + 10,
                ),
            ],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await consume_hold(
            hold.id,
            session,
            HoldOperation(transaction_id=transaction.transaction_id),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        "insufficient_available_balance",
        "Insufficient available balance",
    )


@pytest.mark.asyncio
async def test_consume_hold_function_rejects_non_active_hold(
    session, account_with_existing_hold
):
    _, hold = account_with_existing_hold
    await release_hold(hold.id, session)

    with pytest.raises(HTTPException) as exc:
        await consume_hold(hold.id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "hold_not_active",
        "Only active holds can be consumed",
    )


@pytest.mark.asyncio
async def test_release_hold_function_success(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold

    with freeze_time("2024-01-02T12:00:00Z"):
        result = await release_hold(hold.id, session)

    assert result.hold_id == hold.id
    assert result.status == HoldStatus.RELEASED
    assert hold.released_at == datetime(2024, 1, 2, 12, tzinfo=UTC)
    assert account.balance == 100  # noqa: PLR2004
    assert account.reserved_balance == 0


@pytest.mark.asyncio
async def test_release_hold_function_reuses_idempotency_key(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold
    payload = HoldOperation(idempotency_key="release-hold-1")

    first_result = await release_hold(hold.id, session, payload)
    second_result = await release_hold(hold.id, session, payload)

    assert second_result == first_result
    assert account.balance == 100  # noqa: PLR2004
    assert account.reserved_balance == 0


@pytest.mark.asyncio
async def test_release_hold_function_rejects_non_active_hold(
    session, account_with_existing_hold, other_user_account
):
    _, hold = account_with_existing_hold
    transaction = await create_consume_transaction(
        session, hold, other_user_account
    )
    await consume_hold(
        hold.id,
        session,
        HoldOperation(transaction_id=transaction.transaction_id),
    )

    with pytest.raises(HTTPException) as exc:
        await release_hold(hold.id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        "hold_not_active",
        "Only active holds can be released",
    )


@pytest.mark.asyncio
async def test_expire_hold_function_success(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold

    with freeze_time("2024-01-02T12:00:00Z"):
        result = await expire_hold(hold.id, session)

    assert result.hold_id == hold.id
    assert result.status == HoldStatus.EXPIRED
    assert account.balance == 100  # noqa: PLR2004
    assert account.reserved_balance == 0
    assert hold.released_at == datetime(2024, 1, 2, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_expire_hold_function_reuses_idempotency_key(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold
    payload = HoldOperation(idempotency_key="expire-hold-1")

    first_result = await expire_hold(hold.id, session, payload)
    second_result = await expire_hold(hold.id, session, payload)

    assert second_result == first_result
    assert account.balance == 100  # noqa: PLR2004
    assert account.reserved_balance == 0


@pytest.mark.asyncio
async def test_expire_due_holds_function_expires_only_due_holds(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold
    hold.expires_at = datetime(2024, 1, 2, 11, 59, tzinfo=UTC)
    await session.commit()

    with freeze_time("2024-01-02T12:00:00Z"):
        result = await expire_due_holds(session)

    assert result.expired_count == 1
    assert hold.status == HoldStatus.EXPIRED
    assert account.reserved_balance == 0
    assert hold.released_at == datetime(2024, 1, 2, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_expire_due_holds_in_session_respects_batch_size(
    session, account_with_existing_hold
):
    account, first_hold = account_with_existing_hold
    second_hold = BalanceHold(
        account_id=account.id,
        amount=40,
        expires_at=datetime(2024, 1, 2, 11, 58, tzinfo=UTC),
    )
    first_hold.expires_at = datetime(2024, 1, 2, 11, 59, tzinfo=UTC)
    account.reserved_balance = 80
    session.add(second_hold)
    await session.commit()

    with freeze_time("2024-01-02T12:00:00Z"):
        expired_count = await expire_due_holds_in_session(
            session,
            batch_size=1,
        )

    assert expired_count == 1
    assert second_hold.status == HoldStatus.EXPIRED
    assert first_hold.status == HoldStatus.ACTIVE
    assert account.reserved_balance == 40  # noqa: PLR2004


def test_hold_expiration_worker_parse_args():
    args = hold_expiration_worker.parse_args(
        ["--once", "--interval", "5", "--batch-size", "25"]
    )

    assert args.once is True
    assert args.interval == 5  # noqa: PLR2004
    assert args.batch_size == 25  # noqa: PLR2004


@pytest.mark.asyncio
async def test_hold_expiration_worker_runs_single_batch(monkeypatch):
    calls = []

    async def fake_expire_due_holds_once(*, batch_size):
        calls.append(batch_size)
        return 3

    monkeypatch.setattr(
        hold_expiration_worker,
        "expire_due_holds_once",
        fake_expire_due_holds_once,
    )

    result = await hold_expiration_worker.run_worker(
        interval_seconds=1,
        batch_size=25,
        once=True,
    )

    assert result == 3  # noqa: PLR2004
    assert calls == [25]


@pytest.mark.asyncio
async def test_hold_expiration_worker_returns_zero_when_batch_fails(
    monkeypatch,
):
    async def fake_expire_due_holds_once(*, batch_size):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        hold_expiration_worker,
        "expire_due_holds_once",
        fake_expire_due_holds_once,
    )

    result = await hold_expiration_worker.run_worker(
        interval_seconds=1,
        batch_size=25,
        once=True,
    )

    assert result == 0


@pytest.mark.asyncio
async def test_hold_expiration_worker_sleeps_between_batches(monkeypatch):
    sleep_calls = []

    async def fake_expire_due_holds_once(*, batch_size):
        return 0

    async def fake_sleep(interval_seconds):
        sleep_calls.append(interval_seconds)
        raise RuntimeError("stop worker")

    monkeypatch.setattr(
        hold_expiration_worker,
        "expire_due_holds_once",
        fake_expire_due_holds_once,
    )
    monkeypatch.setattr(
        hold_expiration_worker.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(RuntimeError, match="stop worker"):
        await hold_expiration_worker.run_worker(
            interval_seconds=5,
            batch_size=25,
        )

    assert sleep_calls == [5]


@pytest.mark.asyncio
async def test_hold_expiration_worker_expires_due_holds_once(
    monkeypatch,
    engine,
    session,
    account_with_existing_hold,
):
    account, hold = account_with_existing_hold
    hold.expires_at = datetime(2024, 1, 2, 11, 59, tzinfo=UTC)
    await session.commit()
    monkeypatch.setattr(hold_expiration_worker, "engine", engine)

    with freeze_time("2024-01-02T12:00:00Z"):
        expired_count = await hold_expiration_worker.expire_due_holds_once(
            batch_size=10,
        )

    await session.refresh(account)
    await session.refresh(hold)

    assert expired_count == 1
    assert hold.status == HoldStatus.EXPIRED
    assert account.reserved_balance == 0


@pytest.mark.asyncio
async def test_is_hold_expired_accepts_naive_datetime(
    account_with_existing_hold,
):
    _, hold = account_with_existing_hold
    hold.expires_at = datetime(2024, 1, 1)  # noqa: DTZ001

    with freeze_time("2024-01-02T00:00:00Z"):
        assert is_hold_expired(hold)


@pytest.mark.asyncio
async def test_consume_hold_function_rejects_expired_hold(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold
    hold.expires_at = datetime(2024, 1, 2, 11, 59, tzinfo=UTC)
    await session.commit()

    with (
        pytest.raises(HTTPException) as exc,
        freeze_time("2024-01-02T12:00:00Z"),
    ):
        await consume_hold(hold.id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(exc.value, "hold_expired", "Hold has expired")
    assert hold.status == HoldStatus.EXPIRED
    assert account.reserved_balance == 0
    assert hold.released_at == datetime(2024, 1, 2, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_release_hold_function_rejects_expired_hold(
    session, account_with_existing_hold
):
    account, hold = account_with_existing_hold
    hold.expires_at = datetime(2024, 1, 2, 11, 59, tzinfo=UTC)
    await session.commit()

    with (
        pytest.raises(HTTPException) as exc,
        freeze_time("2024-01-02T12:00:00Z"),
    ):
        await release_hold(hold.id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(exc.value, "hold_expired", "Hold has expired")
    assert hold.status == HoldStatus.EXPIRED
    assert account.reserved_balance == 0
    assert hold.released_at == datetime(2024, 1, 2, 12, tzinfo=UTC)
