import hashlib
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from hub.applications import validate_checkout_application
from hub.ledger_client import LedgerClientError
from hub.models.database import get_session
from hub.models.enums import CheckoutSessionStatus
from hub.routes import checkout as routes
from hub.schemas import CreateCheckoutSessionRequest


@pytest.fixture
def context(monkeypatch):
    application_id = uuid4()
    token = f'lep_{application_id.hex}.test-secret'
    application = SimpleNamespace(
        id=application_id,
        slug='bingo',
        destination_account_id=uuid4(),
        website_url='https://bingo.test',
        key_hash=hashlib.sha256(token.encode()).hexdigest(),
        is_active=True,
    )
    checkout = SimpleNamespace(
        id=uuid4(),
        application_id=application_id,
        status=CheckoutSessionStatus.SETTLED,
        ledger_account_id=uuid4(),
        destination_account_id=application.destination_account_id,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=application),
        scalar=AsyncMock(return_value=checkout),
    )
    ledger = SimpleNamespace(
        create_transaction=AsyncMock(), post_transaction=AsyncMock()
    )
    ledger.create_transaction.return_value = SimpleNamespace(
        transaction_id=uuid4(), status='posted'
    )
    monkeypatch.setattr(routes, 'LedgerClient', lambda: ledger)
    app = FastAPI()
    app.include_router(routes.router)

    async def database():
        yield session

    app.dependency_overrides[get_session] = database
    return SimpleNamespace(
        app=app,
        token=token,
        application=application,
        checkout=checkout,
        session=session,
        ledger=ledger,
    )


def payload():
    return dict(
        payout_id=str(uuid4()),
        order_id=str(uuid4()),
        user_id='alice',
        amount_msat=1001,
    )


async def request(context, data, token=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context.app),
        base_url='https://hub.test',
    ) as client:
        return await client.post(
            '/api/checkout/payouts',
            json=data,
            headers={
                'Authorization': 'Bearer '
                + (context.token if token is None else token)
            },
        )


async def test_prize_uses_checkout_accounts_and_application_scope(context):
    data = payload()
    response = await request(context, data)
    assert response.status_code == HTTPStatus.OK
    assert response.json()['amount_msat'] == data['amount_msat']
    query = context.session.scalar.call_args.args[0]
    values = list(query.compile().params.values())
    assert context.application.id in values
    assert data['order_id'] in values
    assert data['user_id'] in values
    transaction = context.ledger.create_transaction.call_args.args[0]
    assert (
        transaction.entries[0].account_id
        == context.checkout.destination_account_id
    )
    assert (
        transaction.entries[1].account_id == context.checkout.ledger_account_id
    )
    assert all(
        entry.amount_msat == data['amount_msat']
        for entry in transaction.entries
    )


async def test_missing_order_never_transfers(context):
    context.session.scalar.return_value = None
    assert (
        await request(context, payload())
    ).status_code == HTTPStatus.NOT_FOUND
    context.ledger.create_transaction.assert_not_called()


async def test_unsettled_order_never_transfers(context):
    context.checkout.status = CheckoutSessionStatus.CREATED
    assert (
        await request(context, payload())
    ).status_code == HTTPStatus.CONFLICT
    context.ledger.create_transaction.assert_not_called()


async def test_invalid_token_never_transfers(context):
    assert (
        await request(context, payload(), token='invalid')
    ).status_code == HTTPStatus.UNAUTHORIZED
    context.ledger.create_transaction.assert_not_called()


async def test_caller_cannot_supply_ledger_accounts(context):
    assert (
        await request(context, dict(payload(), account_id=str(uuid4())))
    ).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    context.ledger.create_transaction.assert_not_called()


async def test_post_timeout_reuses_transaction_key(context):
    data = payload()
    transaction_id = uuid4()
    context.ledger.create_transaction.side_effect = [
        SimpleNamespace(transaction_id=transaction_id, status='pending'),
        SimpleNamespace(transaction_id=transaction_id, status='posted'),
    ]
    context.ledger.post_transaction.side_effect = LedgerClientError('timeout')
    assert (
        await request(context, data)
    ).status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert (await request(context, data)).json()['transaction_id'] == str(
        transaction_id
    )
    calls = context.ledger.create_transaction.call_args_list
    assert calls[0].args[0] == calls[1].args[0]


def test_checkout_identity_is_resolved_from_application(context):
    checkout = CreateCheckoutSessionRequest(
        user_id='alice',
        order_id='order',
        amount_sats=1000,
        description='Card',
        return_url='https://bingo.test/profile',
        cancel_url='https://bingo.test/profile',
    )
    validate_checkout_application(checkout, context.application)
    assert (
        checkout.destination_account_id
        == context.application.destination_account_id
    )
