from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from hub import applications, main
from hub.checkout_service import (
    CheckoutSessionStateError,
    ensure_application_active,
)
from hub.models.database import get_session
from hub.models.tables import AdminRoleAssignment, ApplicationAudit
from hub.pages import user

ACCOUNT = UUID('00000000-0000-0000-0000-000000000099')
CREATED, OK, UNAUTHORIZED, FORBIDDEN, NOT_FOUND, CONFLICT, INVALID = (
    201,
    200,
    401,
    403,
    404,
    409,
    422,
)


async def register(session, monkeypatch, slug='test-game'):
    monkeypatch.setattr(applications, 'current_user_sub', lambda: 'admin-sub')
    role = await session.scalar(select(AdminRoleAssignment))
    if role is None:
        session.add(AdminRoleAssignment(user_sub='admin-sub', role='admin'))
        await session.commit()
    return await applications.create_application(
        applications.ApplicationCreate(
            name='Test game',
            slug=slug,
            destination_account_id=ACCOUNT,
            website_url='https://game.example',
        ),
        session,
        ledger=SimpleNamespace(get_balance=AsyncMock()),
    )


def payload():
    return {
        'game_id': 'test-game',
        'order_id': 'order-1',
        'destination_account_id': str(ACCOUNT),
        'amount_sats': 100,
        'description': 'Test purchase',
        'return_url': 'https://game.example/success',
        'cancel_url': 'https://game.example/cancel',
    }


@pytest.fixture
async def client(session):
    main.app.dependency_overrides[get_session] = lambda: session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url='http://testserver',
    ) as result:
        yield result
    main.app.dependency_overrides.clear()


async def test_key_lifecycle_and_audit(session, monkeypatch, client):
    application, key = await register(session, monkeypatch)
    assert key not in application.key_hash
    client.headers['Authorization'] = f'Bearer {key}'
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == CREATED
    new_key = await applications.change_application(
        application.id, 'rotate_key', session
    )
    assert new_key != key
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == UNAUTHORIZED
    client.headers['Authorization'] = f'Bearer {new_key}'
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == OK
    await applications.change_application(application.id, 'disabled', session)
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == FORBIDDEN
    with pytest.raises(CheckoutSessionStateError):
        await ensure_application_active(
            SimpleNamespace(application_id=application.id), session
        )
    await applications.change_application(application.id, 'enabled', session)
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == OK
    assert list(
        await session.scalars(
            select(ApplicationAudit.action).order_by(
                ApplicationAudit.created_at
            )
        )
    ) == ['created', 'rotate_key', 'disabled', 'enabled']


async def test_admin_mutations_recheck_role(session, monkeypatch):
    application, _ = await register(session, monkeypatch)
    role = await session.scalar(select(AdminRoleAssignment))
    role.status = 'revoked'
    await session.commit()
    with pytest.raises(HTTPException) as error:
        await applications.change_application(
            application.id, 'disabled', session
        )
    assert error.value.status_code == FORBIDDEN
    assert application.is_active


async def test_application_scope_and_no_direct_debit(
    session, monkeypatch, client
):
    _, key = await register(session, monkeypatch)
    response = await client.post('/api/checkout/sessions', json=payload())
    assert response.status_code == UNAUTHORIZED
    client.headers['Authorization'] = f'Bearer {key}'
    response = await client.post('/api/checkout/sessions', json=payload())
    session_id = response.json()['session_id']
    path = f'/api/checkout/sessions/{session_id}'
    for action in ('prepare', 'settle'):
        assert (
            await client.post(f'{path}/{action}')
        ).status_code == UNAUTHORIZED
    _, other_key = await register(session, monkeypatch, 'other-game')
    client.headers['Authorization'] = f'Bearer {other_key}'
    assert (await client.get(path)).status_code == NOT_FOUND
    assert (await client.post(f'{path}/cancel')).status_code == NOT_FOUND
    assert (
        await client.get('/api/checkout/orders/test-game/order-1')
    ).status_code == NOT_FOUND
    assert (
        await client.post('/api/checkout/sessions', json=payload())
    ).status_code == FORBIDDEN
    client.headers['Authorization'] = f'Bearer {key}'
    assert (await client.post(f'{path}/cancel')).json()['status'] == 'canceled'


@pytest.mark.parametrize(
    ('field', 'value', 'expected'),
    [
        ('game_id', 'other-game', FORBIDDEN),
        (
            'destination_account_id',
            '00000000-0000-0000-0000-000000000098',
            FORBIDDEN,
        ),
        ('return_url', 'https://attacker.example/success', INVALID),
        ('cancel_url', 'https://game.example:8443/cancel', INVALID),
    ],
)
async def test_registered_checkout_constraints(  # noqa: PLR0913, PLR0917
    session, monkeypatch, client, field, value, expected
):
    _, key = await register(session, monkeypatch)
    client.headers['Authorization'] = f'Bearer {key}'
    assert (
        await client.post(
            '/api/checkout/sessions', json={**payload(), field: value}
        )
    ).status_code == expected


async def test_conflicting_retry_does_not_change_charge(
    session, monkeypatch, client
):
    _, key = await register(session, monkeypatch)
    client.headers['Authorization'] = f'Bearer {key}'
    first = await client.post('/api/checkout/sessions', json=payload())
    changed = await client.post(
        '/api/checkout/sessions', json={**payload(), 'amount_sats': 200}
    )
    assert changed.status_code == CONFLICT
    retry = await client.post('/api/checkout/sessions', json=payload())
    assert retry.json()['session_id'] == first.json()['session_id']


async def test_viewing_checkout_does_not_reserve_balance(
    session, monkeypatch, client
):
    _, key = await register(session, monkeypatch)
    client.headers['Authorization'] = f'Bearer {key}'
    response = await client.post('/api/checkout/sessions', json=payload())
    prepare = AsyncMock(
        side_effect=AssertionError('Payment needs confirmation')
    )
    monkeypatch.setattr(user, 'prepare_checkout_payment', prepare)
    record = await user._prepare_checkout_session_for_user(
        session_id=UUID(response.json()['session_id']),
        user_sub='player',
    )
    assert record.status == 'created'
    assert record.user_id is None
    prepare.assert_not_awaited()


@pytest.mark.parametrize('origin', ['', 'https://other.example', 'null'])
def test_confirmation_rejects_foreign_origin(monkeypatch, origin):
    monkeypatch.setattr(applications, 'is_valid_user_info', lambda user: True)
    request = Request({
        'type': 'http',
        'scheme': 'http',
        'server': ('testserver', 80),
        'path': '/',
        'root_path': '',
        'query_string': b'',
        'headers': [(b'origin', origin.encode())],
        'session': {'user_info': {'sub': 'player'}},
    })
    with pytest.raises(HTTPException) as error:
        applications.require_checkout_user(request)
    assert error.value.status_code == FORBIDDEN


def test_confirmation_accepts_authenticated_same_origin(monkeypatch):
    monkeypatch.setattr(applications, 'is_valid_user_info', lambda user: True)
    request = Request({
        'type': 'http',
        'scheme': 'http',
        'server': ('testserver', 80),
        'path': '/',
        'root_path': '',
        'query_string': b'',
        'headers': [(b'origin', b'http://testserver')],
        'session': {'user_info': {'sub': 'player'}},
    })
    assert applications.require_checkout_user(request) == 'player'
