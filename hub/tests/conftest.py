import asyncio
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

from hub.models import database
from hub.models.tables import table_registry

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class SessionScopeContext:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *_args) -> bool:
        return False


@pytest.fixture(scope='session')
def engine():
    with PostgresContainer('postgres:16', driver='psycopg') as postgres:
        _engine = create_async_engine(postgres.get_connection_url())
        yield _engine


@pytest_asyncio.fixture
async def session(engine, monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        monkeypatch.setattr(
            database,
            'async_session_factory',
            lambda: SessionScopeContext(session),
        )
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.drop_all)
