import pytest

from pls.models import database


@pytest.mark.asyncio
async def test_session_scope_yields_session_fixture(session):
    async with database.session_scope() as yielded_session:
        assert yielded_session is session
