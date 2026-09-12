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

from ledger.models.enums import EntryType, TransactionStatus
from ledger.models.tables import (
    LedgerAuditEvent,
    LedgerEntry,
    LedgerTransaction,
)
from ledger.routes import transactions as transactions_module
from ledger.routes.transactions import (
    create_transaction,
    get_transaction,
    post_transaction,
    reverse_transaction,
)
from ledger.schemas import (
    TransactionCreate,
    TransactionEntryCreate,
    TransactionOperation,
)


def assert_error(exc: HTTPException, code: str, message: str) -> None:
    assert exc.detail == {'code': code, 'message': message}


def test_create_transaction(client, user_account, other_user_account):
    response = client.post(
        '/transactions',
        json={
            'reference_type': 'payment_order',
            'reference_id': str(uuid4()),
            'idempotency_key': 'txn-1',
            'description': 'Payment order',
            'entries': [
                {
                    'account_id': str(user_account.id),
                    'entry_type': 'debit',
                    'amount': 100,
                },
                {
                    'account_id': str(other_user_account.id),
                    'entry_type': 'credit',
                    'amount': 100,
                },
            ],
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    transaction_id = response.json().get('transaction_id')
    assert response.json() == {
        'transaction_id': transaction_id,
        'status': 'pending',
    }


@pytest.mark.asyncio
async def test_create_transaction_function_success(
    session, user_account, other_user_account
):
    reference_id = uuid4()

    result = await create_transaction(
        session,
        payload=TransactionCreate(
            reference_type='payment_order',
            reference_id=reference_id,
            idempotency_key='txn-1',
            description='Payment order',
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    transaction = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.id == result.transaction_id
        )
    )
    entries = await session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.transaction_id == result.transaction_id
        )
    )

    assert result.model_dump() == {
        'transaction_id': result.transaction_id,
        'status': TransactionStatus.PENDING,
    }
    assert transaction is not None
    assert transaction.status == TransactionStatus.PENDING
    assert transaction.reference_type == 'payment_order'
    assert transaction.reference_id == reference_id
    assert transaction.idempotency_key == 'txn-1'
    assert transaction.idempotency_payload_hash is not None
    assert transaction.description == 'Payment order'
    assert len(list(entries)) == 2  # noqa: PLR2004
    audit_event = await session.scalar(
        select(LedgerAuditEvent).where(
            LedgerAuditEvent.resource_id == transaction.id,
            LedgerAuditEvent.event_type == 'transaction_created',
        )
    )
    assert audit_event is not None
    assert audit_event.event_metadata['entry_count'] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_transaction_function_reuses_idempotency_key(
    session, user_account, other_user_account
):
    payload = TransactionCreate(
        idempotency_key='txn-1',
        entries=[
            TransactionEntryCreate(
                account_id=user_account.id,
                entry_type=EntryType.DEBIT,
                amount=100,
            ),
            TransactionEntryCreate(
                account_id=other_user_account.id,
                entry_type=EntryType.CREDIT,
                amount=100,
            ),
        ],
    )

    first_result = await create_transaction(session, payload=payload)
    second_result = await create_transaction(session, payload=payload)

    assert second_result == first_result


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_idempotency_payload_mismatch(  # noqa: E501
    session, user_account, other_user_account
):
    payload = TransactionCreate(
        idempotency_key='txn-1',
        entries=[
            TransactionEntryCreate(
                account_id=user_account.id,
                entry_type=EntryType.DEBIT,
                amount=100,
            ),
            TransactionEntryCreate(
                account_id=other_user_account.id,
                entry_type=EntryType.CREDIT,
                amount=100,
            ),
        ],
    )
    await create_transaction(session, payload)

    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            TransactionCreate(
                idempotency_key='txn-1',
                entries=[
                    TransactionEntryCreate(
                        account_id=user_account.id,
                        entry_type=EntryType.DEBIT,
                        amount=200,
                    ),
                    TransactionEntryCreate(
                        account_id=other_user_account.id,
                        entry_type=EntryType.CREDIT,
                        amount=200,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'idempotency_payload_mismatch',
        'Idempotency key was reused with a different payload',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_unbalanced_entries(
    session, user_account, other_user_account
):
    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(
                entries=[
                    TransactionEntryCreate(
                        account_id=user_account.id,
                        entry_type=EntryType.DEBIT,
                        amount=100,
                    ),
                    TransactionEntryCreate(
                        account_id=other_user_account.id,
                        entry_type=EntryType.CREDIT,
                        amount=50,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        'transaction_unbalanced',
        'Transaction entries must be balanced',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_empty_entries(session):
    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(description='metadata only'),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        'transaction_entries_required',
        'Transaction must have at least one entry',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_zero_amount(
    session, user_account, other_user_account
):
    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(
                entries=[
                    TransactionEntryCreate(
                        account_id=user_account.id,
                        entry_type=EntryType.DEBIT,
                        amount=0,
                    ),
                    TransactionEntryCreate(
                        account_id=other_user_account.id,
                        entry_type=EntryType.CREDIT,
                        amount=0,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        'invalid_entry_amount',
        'Entry amount must be greater than zero',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_missing_account(session):
    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(
                entries=[
                    TransactionEntryCreate(
                        account_id=uuid4(),
                        entry_type=EntryType.DEBIT,
                        amount=100,
                    ),
                    TransactionEntryCreate(
                        account_id=uuid4(),
                        entry_type=EntryType.CREDIT,
                        amount=100,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert_error(exc.value, 'account_not_found', 'Account not found')


@pytest.mark.asyncio
async def test_create_transaction_function_handles_idempotency_commit_conflict(
    monkeypatch, session, user_account, other_user_account
):
    async def fake_commit():
        raise IntegrityError('insert', {}, Exception('duplicate key'))

    monkeypatch.setattr(session, 'commit', fake_commit)

    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(
                idempotency_key='txn-1',
                entries=[
                    TransactionEntryCreate(
                        account_id=user_account.id,
                        entry_type=EntryType.DEBIT,
                        amount=100,
                    ),
                    TransactionEntryCreate(
                        account_id=other_user_account.id,
                        entry_type=EntryType.CREDIT,
                        amount=100,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_idempotency_conflict',
        'Transaction idempotency key already exists',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_returns_existing_after_integrity_error(  # noqa: E501
    session,
    user_account,
    other_user_account,
    existing_transaction_after_integrity_error,
):
    result = await create_transaction(
        session,
        payload=TransactionCreate(
            idempotency_key='txn-1',
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    assert (
        result.transaction_id == existing_transaction_after_integrity_error.id
    )
    assert result.status == TransactionStatus.PENDING


@pytest.mark.asyncio
async def test_create_transaction_function_returns_existing_after_commit_conflict(  # noqa: E501
    session, user_account_with_balance, other_user_account, transaction
):
    result = await create_transaction(
        session,
        payload=TransactionCreate(
            idempotency_key='txn-1',
            entries=[
                TransactionEntryCreate(
                    account_id=user_account_with_balance.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    assert result.transaction_id == transaction.id
    assert result.status == TransactionStatus.PENDING


@pytest.mark.asyncio
async def test_create_transaction_function_rejects_commit_conflict_payload_mismatch(  # noqa: E501
    monkeypatch, session, user_account_with_balance, other_user_account
):
    existing_transaction = SimpleNamespace(
        id=uuid4(),
        status=TransactionStatus.PENDING,
        idempotency_payload_hash='different',
    )
    scalar_results = iter([None, existing_transaction])

    async def fake_scalar(query):
        return next(scalar_results)

    async def fake_commit():
        raise IntegrityError('insert', {}, Exception('duplicate key'))

    monkeypatch.setattr(session, 'scalar', fake_scalar)
    monkeypatch.setattr(session, 'commit', fake_commit)

    with pytest.raises(HTTPException) as exc:
        await create_transaction(
            session,
            payload=TransactionCreate(
                idempotency_key='txn-1',
                entries=[
                    TransactionEntryCreate(
                        account_id=user_account_with_balance.id,
                        entry_type=EntryType.DEBIT,
                        amount=100,
                    ),
                    TransactionEntryCreate(
                        account_id=other_user_account.id,
                        entry_type=EntryType.CREDIT,
                        amount=100,
                    ),
                ],
            ),
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'idempotency_payload_mismatch',
        'Idempotency key was reused with a different payload',
    )


@pytest.mark.asyncio
async def test_create_transaction_function_returns_existing_when_commit_fails(
    session, user_account_with_balance, other_user_account, transaction
):
    result = await create_transaction(
        session,
        payload=TransactionCreate(
            idempotency_key='txn-1',
            entries=[
                TransactionEntryCreate(
                    account_id=user_account_with_balance.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    assert result.transaction_id == transaction.id
    assert result.status == TransactionStatus.PENDING


@pytest.mark.asyncio
async def test_ensure_accounts_exist_accepts_empty_entries():
    assert await transactions_module._ensure_accounts_exist([], None) == {}


@pytest.mark.asyncio
async def test_get_transaction_function_success(
    session, user_account, other_user_account
):
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            reference_type='payment_order',
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    result = await get_transaction(created.transaction_id, session)

    assert result.transaction_id == created.transaction_id
    assert result.status == TransactionStatus.PENDING
    assert result.reference_type == 'payment_order'
    assert len(result.entries) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_transaction_function_not_found(session):
    with pytest.raises(HTTPException) as exc:
        await get_transaction(uuid4(), session)

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert_error(
        exc.value,
        'transaction_not_found',
        'Transaction not found',
    )


@pytest.mark.asyncio
async def test_post_transaction_function_success(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    with freeze_time('2024-01-02T12:00:00Z'):
        result = await post_transaction(created.transaction_id, session)
    transaction = await session.get(LedgerTransaction, created.transaction_id)

    assert result.status == TransactionStatus.POSTED
    assert transaction is not None
    assert transaction.posted_at == datetime(2024, 1, 2, 12, tzinfo=UTC)
    audit_event = await session.scalar(
        select(LedgerAuditEvent).where(
            LedgerAuditEvent.resource_id == transaction.id,
            LedgerAuditEvent.event_type == 'transaction_posted',
        )
    )
    assert audit_event is not None
    assert user_account.balance == 0
    assert other_user_account.balance == 100  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_transaction_function_reuses_idempotency_key(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    payload = TransactionOperation(idempotency_key='post-txn-1')

    first_result = await post_transaction(
        created.transaction_id, session, payload
    )
    second_result = await post_transaction(
        created.transaction_id, session, payload
    )

    assert second_result == first_result
    assert user_account.balance == 0
    assert other_user_account.balance == 100  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_transaction_function_rejects_insufficient_balance(
    session, user_account, other_user_account
):
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await post_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        exc.value,
        'insufficient_available_balance',
        'Insufficient available balance',
    )


@pytest.mark.asyncio
async def test_post_transaction_concurrent_requests_cannot_overspend(
    engine, session, user_account_with_balance, other_user_account
):
    first = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account_with_balance.id,
                    entry_type=EntryType.DEBIT,
                    amount=80,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=80,
                ),
            ],
        ),
    )
    second = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account_with_balance.id,
                    entry_type=EntryType.DEBIT,
                    amount=80,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=80,
                ),
            ],
        ),
    )

    async def post_competing_transaction(transaction_id):
        async with AsyncSession(
            engine, expire_on_commit=False
        ) as local_session:
            try:
                return await post_transaction(transaction_id, local_session)
            except HTTPException as exc:
                return exc

    results = await asyncio.gather(
        post_competing_transaction(first.transaction_id),
        post_competing_transaction(second.transaction_id),
    )

    posted = [
        result
        for result in results
        if not isinstance(result, Exception)
        and result.status == TransactionStatus.POSTED
    ]
    rejected = [
        result for result in results if isinstance(result, HTTPException)
    ]

    assert len(posted) == 1
    assert len(rejected) == 1
    assert rejected[0].status_code == status.HTTP_400_BAD_REQUEST
    assert_error(
        rejected[0],
        'insufficient_available_balance',
        'Insufficient available balance',
    )


@pytest.mark.asyncio
async def test_post_transaction_function_rejects_non_pending_transaction(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)

    with pytest.raises(HTTPException) as exc:
        await post_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_not_pending',
        'Only pending transactions can be posted',
    )


@pytest.mark.asyncio
async def test_reverse_transaction_function_success(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)

    with freeze_time('2024-01-02T13:00:00Z'):
        result = await reverse_transaction(created.transaction_id, session)
    reversal = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.reversed_transaction_id == created.transaction_id
        )
    )

    assert result.status == TransactionStatus.REVERSED
    assert reversal is not None
    assert reversal.status == TransactionStatus.POSTED
    assert reversal.posted_at == datetime(2024, 1, 2, 13, tzinfo=UTC)
    assert user_account.balance == 100  # noqa: PLR2004
    assert other_user_account.balance == 0


@pytest.mark.asyncio
async def test_reverse_transaction_function_reuses_idempotency_key(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)
    payload = TransactionOperation(idempotency_key='reverse-txn-1')

    first_result = await reverse_transaction(
        created.transaction_id, session, payload
    )
    second_result = await reverse_transaction(
        created.transaction_id, session, payload
    )

    assert second_result == first_result
    assert user_account.balance == 100  # noqa: PLR2004
    assert other_user_account.balance == 0


@pytest.mark.asyncio
async def test_reverse_transaction_function_rejects_pending_transaction(
    session, user_account, other_user_account
):
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await reverse_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_not_posted',
        'Only posted transactions can be reversed',
    )


@pytest.mark.asyncio
async def test_reverse_transaction_function_rejects_existing_reversal(
    session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)

    transaction = await session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.id == created.transaction_id
        )
    )
    existing_reversal = LedgerTransaction(status=TransactionStatus.POSTED)
    existing_reversal.reversed_transaction = transaction
    session.add(existing_reversal)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await reverse_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_already_reversed',
        'Transaction already reversed',
    )


@pytest.mark.asyncio
async def test_reverse_transaction_function_handles_reversal_create_conflict(
    monkeypatch, session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)

    async def fake_create_reversal(transaction, session):
        raise IntegrityError('insert', {}, Exception('duplicate key'))

    monkeypatch.setattr(
        transactions_module,
        '_create_reversal_transaction',
        fake_create_reversal,
    )

    with pytest.raises(HTTPException) as exc:
        await reverse_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_already_reversed',
        'Transaction already reversed',
    )


@pytest.mark.asyncio
async def test_reverse_transaction_function_handles_commit_conflict(
    monkeypatch, session, user_account, other_user_account
):
    user_account.balance = 100
    await session.commit()
    created = await create_transaction(
        session,
        payload=TransactionCreate(
            entries=[
                TransactionEntryCreate(
                    account_id=user_account.id,
                    entry_type=EntryType.DEBIT,
                    amount=100,
                ),
                TransactionEntryCreate(
                    account_id=other_user_account.id,
                    entry_type=EntryType.CREDIT,
                    amount=100,
                ),
            ],
        ),
    )
    await post_transaction(created.transaction_id, session)

    async def fake_commit():
        raise IntegrityError('update', {}, Exception('duplicate key'))

    monkeypatch.setattr(session, 'commit', fake_commit)

    with pytest.raises(HTTPException) as exc:
        await reverse_transaction(created.transaction_id, session)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert_error(
        exc.value,
        'transaction_already_reversed',
        'Transaction already reversed',
    )
