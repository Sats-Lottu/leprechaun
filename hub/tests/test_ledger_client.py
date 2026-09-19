from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from hub.ledger_client import (
    LedgerClient,
    LedgerClientError,
    LedgerTransactionCreate,
    LedgerTransactionEntryCreate,
)

BALANCE_MSAT = 3000
RESERVED_MSAT = 1000
AVAILABLE_MSAT = 2000
LISTED_AVAILABLE_MSAT = 5000
HOLD_AMOUNT_MSAT = 1500
STATEMENT_AMOUNT_MSAT = 2000
ACCOUNT_PATH = '/accounts/00000000-0000-0000-0000-000000000001'
HOLDS_PATH = '/holds/account/00000000-0000-0000-0000-000000000001'
STATEMENT_PATH = f'{ACCOUNT_PATH}/statement'
RELEASE_PATH = '/holds/00000000-0000-0000-0000-000000000010/release'
TRANSACTION_PATH = '/transactions'
TRANSACTION_POST_PATH = (
    '/transactions/00000000-0000-0000-0000-000000000020/post'
)
HOLD_CONSUME_PATH = '/holds/00000000-0000-0000-0000-000000000010/consume'


@pytest.mark.asyncio
async def test_ledger_client_gets_balance(monkeypatch) -> None:
    requests = []
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                'balance': BALANCE_MSAT,
                'reserved_balance': RESERVED_MSAT,
                'available_balance': AVAILABLE_MSAT,
            },
        )

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    client = LedgerClient(base_url='http://ledger', token='token')
    balance = await client.get_balance(
        UUID('00000000-0000-0000-0000-000000000001')
    )

    assert balance.balance_msat == BALANCE_MSAT
    assert balance.reserved_balance_msat == RESERVED_MSAT
    assert balance.available_balance_msat == AVAILABLE_MSAT
    assert requests[0].headers['X-Internal-Service-Token'] == 'token'


@pytest.mark.asyncio
async def test_ledger_client_lists_accounts(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'accounts': [
                    {
                        'account_id': (
                            '00000000-0000-0000-0000-000000000001'
                        ),
                        'account_type': 'user',
                        'owner_id': None,
                        'name': 'Alice',
                        'balance': 8_000,
                        'reserved_balance': 3_000,
                        'available_balance': LISTED_AVAILABLE_MSAT,
                        'is_active': True,
                    }
                ]
            },
        )

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    accounts = await LedgerClient(base_url='http://ledger').list_accounts()

    assert accounts[0].name == 'Alice'
    assert accounts[0].available_balance_msat == LISTED_AVAILABLE_MSAT


@pytest.mark.asyncio
async def test_ledger_client_raises_for_error(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='ledger failed')

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    client = LedgerClient(base_url='http://ledger')

    with pytest.raises(LedgerClientError):
        await client.get_balance(
            UUID('00000000-0000-0000-0000-000000000001')
        )


@pytest.mark.asyncio
async def test_ledger_client_lists_holds_and_statement(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(HOLDS_PATH):
            return httpx.Response(
                200,
                json={
                    'account_id': '00000000-0000-0000-0000-000000000001',
                    'holds': [
                        {
                            'hold_id': '00000000-0000-0000-0000-000000000010',
                            'account_id': (
                                '00000000-0000-0000-0000-000000000001'
                            ),
                            'amount': HOLD_AMOUNT_MSAT,
                            'status': 'active',
                            'reason': 'checkout',
                            'reference_type': 'checkout_session',
                            'reference_id': (
                                '00000000-0000-0000-0000-000000000020'
                            ),
                            'expires_at': '2026-04-21T12:00:00+00:00',
                            'consumed_at': None,
                            'released_at': None,
                        }
                    ],
                },
            )
        if request.url.path.endswith(STATEMENT_PATH):
            return httpx.Response(
                200,
                json={
                    'entries': [
                        {
                            'account_id': (
                                '00000000-0000-0000-0000-000000000001'
                            ),
                            'entry_type': 'credit',
                            'amount': STATEMENT_AMOUNT_MSAT,
                            'description': 'deposit',
                            'reference_type': 'payment.invoice.paid',
                            'reference_id': (
                                '00000000-0000-0000-0000-000000000021'
                            ),
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    client = LedgerClient(base_url='http://ledger')
    account_id = UUID('00000000-0000-0000-0000-000000000001')

    holds = await client.list_holds(account_id)
    statement = await client.get_statement(account_id)

    assert holds[0].amount_msat == HOLD_AMOUNT_MSAT
    assert holds[0].expires_at == datetime(
        2026, 4, 21, 12, 0, tzinfo=timezone.utc
    )
    assert statement[0].entry_type == 'credit'
    assert statement[0].amount_msat == STATEMENT_AMOUNT_MSAT


@pytest.mark.asyncio
async def test_ledger_client_releases_hold(monkeypatch) -> None:
    requests = []
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                'hold_id': '00000000-0000-0000-0000-000000000010',
                'status': 'released',
            },
        )

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    client = LedgerClient(base_url='http://ledger')
    hold = await client.release_hold(
        UUID('00000000-0000-0000-0000-000000000010'),
        idempotency_key='checkout:1:cancel',
    )

    assert hold.status == 'released'
    assert requests[0].method == 'POST'
    assert requests[0].url.path.endswith(RELEASE_PATH)


@pytest.mark.asyncio
async def test_ledger_client_creates_posts_transaction_and_consumes_hold(
    monkeypatch,
) -> None:
    requests = []
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if (
            request.url.path.endswith(TRANSACTION_PATH)
            and request.method == 'POST'
        ):
            return httpx.Response(
                201,
                json={
                    'transaction_id': '00000000-0000-0000-0000-000000000020',
                    'status': 'pending',
                },
            )
        if request.url.path.endswith(TRANSACTION_POST_PATH):
            return httpx.Response(
                200,
                json={
                    'transaction_id': '00000000-0000-0000-0000-000000000020',
                    'status': 'posted',
                },
            )
        if request.url.path.endswith(HOLD_CONSUME_PATH):
            return httpx.Response(
                200,
                json={
                    'hold_id': '00000000-0000-0000-0000-000000000010',
                    'status': 'consumed',
                },
            )
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    client = LedgerClient(base_url='http://ledger')
    txn = await client.create_transaction(
        LedgerTransactionCreate(
            reference_type='checkout_session',
            reference_id=UUID('00000000-0000-0000-0000-000000000021'),
            idempotency_key='checkout:1:settle:internal',
            description='Checkout internal settlement',
            entries=(
                LedgerTransactionEntryCreate(
                    account_id=UUID('00000000-0000-0000-0000-000000000001'),
                    entry_type='debit',
                    amount_msat=1_000,
                ),
                LedgerTransactionEntryCreate(
                    account_id=UUID('00000000-0000-0000-0000-000000000002'),
                    entry_type='credit',
                    amount_msat=1_000,
                ),
            ),
        )
    )
    posted = await client.post_transaction(
        txn.transaction_id,
        idempotency_key='checkout:1:post:external',
    )
    hold = await client.consume_hold(
        UUID('00000000-0000-0000-0000-000000000010'),
        transaction_id=txn.transaction_id,
        idempotency_key='checkout:1:consume:internal',
    )

    assert txn.status == 'pending'
    assert posted.status == 'posted'
    assert hold.status == 'consumed'
