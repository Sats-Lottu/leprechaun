from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import factory
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

from ledger.main import app
from ledger.models.database import get_session
from ledger.models.enums import AccountType
from ledger.models.tables import (
    Account,
    BalanceHold,
    LedgerTransaction,
    table_registry,
)
from ledger.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(session):
    def get_session_override():
        return session

    with TestClient(app) as client:
        app.dependency_overrides[get_session] = get_session_override
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def engine():
    with PostgresContainer("postgres:16", driver="psycopg") as postgres:
        _engine = create_async_engine(postgres.get_connection_url())
        yield _engine


@pytest_asyncio.fixture
async def session(engine):
    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.drop_all)


@contextmanager
def _mock_db_time(*, model, time=datetime(2024, 1, 1, tzinfo=UTC)):
    def fake_time_handler(mapper, connection, target):
        if hasattr(target, "created_at"):
            target.created_at = time
        if hasattr(target, "updated_at"):
            target.updated_at = time

    event.listen(model, "before_insert", fake_time_handler)

    yield time

    event.remove(model, "before_insert", fake_time_handler)


@pytest.fixture
def mock_db_time():
    return _mock_db_time


@pytest_asyncio.fixture
async def user_account(session):
    user = AccountFactory(owner_id=uuid4())
    session.add(user)
    await session.commit()
    await session.refresh(user)

    return user


@pytest_asyncio.fixture
async def user_account_with_balance(session):
    user = AccountFactory(owner_id=uuid4(), balance=100)
    session.add(user)
    await session.commit()
    await session.refresh(user)

    return user


@pytest_asyncio.fixture
async def other_user_account(session):
    user_ = AccountFactory(owner_id=uuid4())

    session.add(user_)
    await session.commit()
    await session.refresh(user_)

    return user_


@pytest_asyncio.fixture
async def transaction(session):
    transaction_ = LedgerTransaction(idempotency_key="txn-1")
    session.add(transaction_)
    await session.commit()
    await session.refresh(transaction_)

    return transaction_


@pytest.fixture
def existing_transaction_after_integrity_error(monkeypatch, session):
    existing_transaction = LedgerTransaction(idempotency_key="txn-1")
    scalar_results = iter([None, existing_transaction])

    async def fake_scalar(query):
        return next(scalar_results)

    async def fake_commit():
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(session, "scalar", fake_scalar)
    monkeypatch.setattr(session, "commit", fake_commit)

    return existing_transaction


@pytest_asyncio.fixture
async def account_with_existing_hold(session) -> tuple[Account, BalanceHold]:
    account = Account(balance=100, reserved_balance=40)
    hold = BalanceHold(
        account_id=account.id, amount=40, idempotency_key="hold-1"
    )
    session.add(account)
    session.add(hold)
    await session.commit()
    await session.refresh(account)
    await session.refresh(hold)
    return account, hold


class AccountFactory(factory.Factory):
    class Meta:
        model = Account

    account_type = factory.LazyAttribute(lambda _: AccountType.USER)
    owner_id = factory.LazyAttribute(lambda id: UUID(id.hex) if id else None)
    name = factory.Sequence(lambda n: f"Account {n}")
    balance = factory.LazyAttribute(lambda _: 0)
    reserved_balance = factory.LazyAttribute(lambda _: 0)
