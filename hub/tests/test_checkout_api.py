from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from hub import main
from hub.applications import require_application, require_checkout_user
from hub.models.database import get_session
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession
from hub.routes import checkout
from hub.schemas import MSATS_PER_SAT, CreateCheckoutSessionRequest

AMOUNT_SATS = 1000
AMOUNT_MSAT = AMOUNT_SATS * MSATS_PER_SAT
DESTINATION_ACCOUNT_ID = '00000000-0000-0000-0000-000000000099'
CREATED = 201
OK = 200
CONFLICT = 409


@pytest.fixture(autouse=True)
def authorized_checkout_dependencies():
    main.app.dependency_overrides[require_application] = lambda: (
        SimpleNamespace(
            id=None,
            slug='leprechaun-game',
            destination_account_id=UUID(DESTINATION_ACCOUNT_ID),
            website_url='https://game.example/',
        )
    )
    main.app.dependency_overrides[require_checkout_user] = lambda: 'user-123'
    yield
    main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_session_builds_checkout_session(session) -> None:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    payload = CreateCheckoutSessionRequest(
        user_id='user-123',
        game_id='leprechaun-game',
        order_id='order-1',
        destination_account_id=DESTINATION_ACCOUNT_ID,
        amount_sats=AMOUNT_SATS,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        expires_in_sec=60,
    )

    result = await checkout.create_session(
        payload,
        base_url='http://hub.local/',
        session=session,
        now=now,
    )

    assert result.created
    assert result.session.status == 'created'
    assert result.session.user_id == 'user-123'
    assert result.session.game_id == 'leprechaun-game'
    assert result.session.order_id == 'order-1'
    assert result.session.destination_account_id == DESTINATION_ACCOUNT_ID
    assert result.session.amount_msat == AMOUNT_MSAT
    assert result.session.checkout_url == (
        'http://hub.local/user/checkout'
        f'?session_id={result.session.session_id}'
    )
    assert result.session.created_at
    assert result.session.expires_at.startswith('2026-04-20T12:01:00')

    stored_session = await session.scalar(select(CheckoutSession))
    assert stored_session is not None
    assert stored_session.amount_msat == AMOUNT_MSAT
    assert str(stored_session.destination_account_id) == DESTINATION_ACCOUNT_ID


@pytest.mark.asyncio
async def test_create_session_is_idempotent_by_game_and_order(session) -> None:
    payload = CreateCheckoutSessionRequest(
        game_id='leprechaun-game',
        order_id='order-1',
        destination_account_id=DESTINATION_ACCOUNT_ID,
        amount_sats=AMOUNT_SATS,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
    )

    first = await checkout.create_session(
        payload,
        base_url='http://hub.local',
        session=session,
    )
    second = await checkout.create_session(
        payload,
        base_url='http://hub.local',
        session=session,
    )

    assert first.created
    assert not second.created
    assert second.session.session_id == first.session.session_id


@pytest.mark.asyncio
async def test_checkout_session_endpoint_creates_session(session) -> None:
    main.app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            response = await client.post(
                '/api/checkout/sessions',
                json={
                    'user_id': 'user-123',
                    'game_id': 'leprechaun-game',
                    'order_id': 'order-1',
                    'destination_account_id': DESTINATION_ACCOUNT_ID,
                    'amount_sats': AMOUNT_SATS,
                    'description': 'Ticket purchase',
                    'return_url': 'https://game.example/return',
                    'cancel_url': 'https://game.example/cancel',
                },
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == CREATED
    data = response.json()
    assert data['status'] == 'created'
    assert data['user_id'] == 'user-123'
    assert data['game_id'] == 'leprechaun-game'
    assert data['order_id'] == 'order-1'
    assert data['destination_account_id'] == DESTINATION_ACCOUNT_ID
    assert data['amount_sats'] == AMOUNT_SATS
    assert 'amount_msat' not in data
    assert data['checkout_url'].startswith(
        'http://testserver/user/checkout?session_id='
    )


@pytest.mark.asyncio
async def test_checkout_session_endpoint_returns_existing_session(
    session,
) -> None:
    main.app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=main.app)
    payload = {
        'game_id': 'leprechaun-game',
        'order_id': 'order-1',
        'destination_account_id': DESTINATION_ACCOUNT_ID,
        'amount_sats': AMOUNT_SATS,
        'description': 'Ticket purchase',
        'return_url': 'https://game.example/return',
        'cancel_url': 'https://game.example/cancel',
    }

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            first = await client.post('/api/checkout/sessions', json=payload)
            second = await client.post('/api/checkout/sessions', json=payload)
    finally:
        main.app.dependency_overrides.clear()

    assert first.status_code == CREATED
    assert second.status_code == OK
    assert second.json()['session_id'] == first.json()['session_id']


@pytest.mark.asyncio
async def test_checkout_order_endpoint_returns_session_by_game_and_order(
    session,
) -> None:
    result = await checkout.create_session(
        CreateCheckoutSessionRequest(
            game_id='leprechaun-game',
            order_id='order-1',
            destination_account_id=DESTINATION_ACCOUNT_ID,
            amount_sats=AMOUNT_SATS,
            description='Ticket purchase',
            return_url='https://game.example/return',
            cancel_url='https://game.example/cancel',
        ),
        base_url='http://hub.local',
        session=session,
    )
    main.app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            response = await client.get(
                '/api/checkout/orders/leprechaun-game/order-1'
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == OK
    data = response.json()
    assert data['session_id'] == result.session.session_id
    assert data['game_id'] == 'leprechaun-game'
    assert data['order_id'] == 'order-1'


@pytest.mark.asyncio
async def test_prepare_endpoint_rejects_expired_checkout_before_ledger(
    session,
    monkeypatch,
) -> None:
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    session.add(checkout_session)
    await session.commit()
    await session.refresh(checkout_session)
    main.app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            response = await client.post(
                f'/api/checkout/sessions/{checkout_session.id}/prepare'
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == CONFLICT
    assert response.json()['detail'] == 'Checkout session is expired'


@pytest.mark.asyncio
async def test_cancel_endpoint_does_not_overwrite_settled_session(
    session,
) -> None:
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        status=CheckoutSessionStatus.SETTLED,
    )
    session.add(checkout_session)
    await session.commit()
    await session.refresh(checkout_session)
    main.app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            response = await client.post(
                f'/api/checkout/sessions/{checkout_session.id}/cancel'
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == CONFLICT
    assert response.json()['detail'] == 'Checkout session is settled'


@pytest.mark.asyncio
async def test_settle_endpoint_returns_settled_checkout(monkeypatch) -> None:
    checkout_session = SimpleNamespace(
        id='00000000-0000-0000-0000-000000000123',
        status=CheckoutSessionStatus.SETTLED,
        user_id='user-123',
        game_id='leprechaun-game',
        order_id='order-1',
        destination_account_id=DESTINATION_ACCOUNT_ID,
        amount_msat=AMOUNT_MSAT,
        internal_amount_msat=4_000,
        external_amount_msat=AMOUNT_MSAT - 4_000,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        ledger_hold_id=None,
        invoice_id='invoice-1',
        payment_request='lnbc1',
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    monkeypatch.setattr(
        checkout,
        'settle_checkout_payment',
        AsyncMock(return_value=checkout_session),
    )

    def override_session():
        return SimpleNamespace(scalar=AsyncMock(return_value=checkout_session))

    main.app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://testserver',
        ) as client:
            response = await client.post(
                '/api/checkout/sessions/00000000-0000-0000-0000-000000000123/settle'
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == OK
    data = response.json()
    assert data['status'] == 'settled'
    assert data['destination_account_id'] == DESTINATION_ACCOUNT_ID
