from dataclasses import asdict
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models import database
from ledger.models.enums import AccountType
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerAuditEvent,
    LedgerEntry,
    LedgerTransaction,
)


@pytest.mark.asyncio
async def test_create_account(session, mock_db_time):
    owner_id = uuid4()
    with mock_db_time(model=Account) as time:
        new_account = Account(
            account_type=AccountType.USER,
            owner_id=owner_id,
            name="alice",
        )
        session.add(new_account)
        await session.commit()

    account_ = await session.scalar(
        select(Account).where(Account.name == "alice")
    )

    assert asdict(account_) == {
        "id": account_.id,
        "account_type": AccountType.USER,
        "owner_id": owner_id,
        "name": "alice",
        "balance": 0,
        "reserved_balance": 0,
        "is_active": True,
        "created_at": time,
        "updated_at": time,
        "entries": [],
        "holds": [],
    }


@pytest.mark.asyncio
async def test_get_session_yields_async_session(engine, monkeypatch):
    monkeypatch.setattr(database, "engine", engine)

    session_generator = database.get_session()
    session = await anext(session_generator)

    assert isinstance(session, AsyncSession)
    assert session.bind is engine
    assert session.sync_session.expire_on_commit is False

    await session_generator.aclose()


@pytest.mark.asyncio
async def test_account_balance_constraints(session):
    account = Account(
        account_type=AccountType.USER,
        owner_id=uuid4(),
        balance=10,
        reserved_balance=20,
    )
    session.add(account)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_account_balance_cannot_be_negative(session):
    account = Account(
        account_type=AccountType.USER,
        owner_id=uuid4(),
        balance=-1,
    )
    session.add(account)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_account_reserved_balance_cannot_be_negative(session):
    account = Account(
        account_type=AccountType.USER,
        owner_id=uuid4(),
        balance=10,
        reserved_balance=-1,
    )
    session.add(account)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_balance_hold_amount_constraint(session, user_account):
    hold = BalanceHold(account_id=user_account.id, amount=0)
    session.add(hold)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_ledger_entry_amount_constraint(session, user_account):
    transaction = LedgerTransaction()
    session.add(transaction)
    await session.flush()

    entry = LedgerEntry(
        transaction_id=transaction.id,
        account_id=user_account.id,
        entry_type="debit",
        amount=0,
    )
    session.add(entry)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_account_type_constraint(session):
    account = Account(account_type="invalid", owner_id=uuid4())
    session.add(account)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_transaction_status_constraint(session):
    transaction = LedgerTransaction(status="invalid")
    session.add(transaction)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_balance_hold_status_constraint(session, user_account):
    hold = BalanceHold(account_id=user_account.id, amount=1, status="invalid")
    session.add(hold)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_ledger_entry_type_constraint(session, user_account):
    transaction = LedgerTransaction()
    session.add(transaction)
    await session.flush()

    entry = LedgerEntry(
        transaction_id=transaction.id,
        account_id=user_account.id,
        entry_type="invalid",
        amount=1,
    )
    session.add(entry)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_create_audit_event(session):
    event = LedgerAuditEvent(
        event_type="account_created",
        resource_type="account",
        resource_id=uuid4(),
        service_name="ledger",
        event_metadata={"amount": 100},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    assert event.id is not None
    assert event.event_metadata == {"amount": 100}
