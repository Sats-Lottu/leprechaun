import json
import logging
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from ledger.models.enums import AccountType
from ledger.models.tables import LedgerEntry
from ledger.observability import JsonFormatter
from ledger.routes.accounts import (
    create_account,
    get_account_balance,
    get_account_statement,
)
from ledger.settings import get_settings


def test_root_returns_service_message(client):
    response = client.get('/')

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {'message': 'Ledger service'}


def test_metrics_endpoint(client):
    response = client.get('/metrics')

    assert response.status_code == status.HTTP_200_OK
    assert 'ledger_http_requests_total' in response.text


def test_list_accounts_returns_balances(client, user_account):
    response = client.get('/accounts')

    assert response.status_code == status.HTTP_200_OK
    account = next(
        item
        for item in response.json()['accounts']
        if item['account_id'] == str(user_account.id)
    )
    assert account['account_type'] == 'user'
    assert account['balance'] == user_account.balance
    assert account['reserved_balance'] == user_account.reserved_balance


def test_internal_auth_rejects_missing_token(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'pls:test-token:read,write',
    )
    get_settings.cache_clear()

    response = client.post(
        '/accounts',
        params={
            'account_type': AccountType.USER,
            'owner_id': uuid4(),
            'name': 'Test Account',
        },
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {
        'detail': {
            'code': 'missing_internal_token',
            'message': 'Internal service token is required',
        }
    }


def test_internal_auth_rejects_invalid_token(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'pls:test-token:read,write',
    )
    get_settings.cache_clear()

    response = client.get(
        '/accounts/00000000-0000-0000-0000-000000000001/balance',
        headers={'X-Internal-Service-Token': 'wrong-token'},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {
        'detail': {
            'code': 'invalid_internal_token',
            'message': 'Internal service token is invalid',
        }
    }


def test_internal_auth_allows_read_scope_for_get(
    client, monkeypatch, user_account
):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'viewer:read-token:read',
    )
    get_settings.cache_clear()

    response = client.get(
        f'/accounts/{user_account.id}/balance',
        headers={'Authorization': 'Bearer read-token'},
    )

    assert response.status_code == status.HTTP_200_OK


def test_internal_auth_ignores_malformed_token_config(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'invalid-config;pls:test-token:read,write',
    )
    get_settings.cache_clear()

    response = client.get(
        '/accounts/00000000-0000-0000-0000-000000000001/balance',
        headers={'X-Internal-Service-Token': 'test-token'},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_internal_auth_defaults_empty_scope_to_read(client, monkeypatch):
    monkeypatch.setenv('LEDGER_INTERNAL_API_TOKENS', 'viewer:read-token:')
    get_settings.cache_clear()

    response = client.get(
        '/accounts/00000000-0000-0000-0000-000000000001/balance',
        headers={'X-Internal-Service-Token': 'read-token'},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_json_formatter_ignores_private_record_fields():
    record = logging.LogRecord(
        'ledger.test',
        logging.INFO,
        'test_accounts.py',
        1,
        'test message',
        (),
        None,
    )
    record._private = 'hidden'
    record._ledger_extra = {'event': 'test_event'}

    formatted = json.loads(JsonFormatter().format(record))

    assert formatted['event'] == 'test_event'
    assert '_private' not in formatted


def test_internal_auth_allows_service_token(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'pls:test-token:read,write',
    )
    get_settings.cache_clear()
    owner_id = uuid4()

    response = client.post(
        '/accounts',
        headers={'X-Internal-Service-Token': 'test-token'},
        params={
            'account_type': AccountType.USER,
            'owner_id': owner_id,
            'name': 'Test Account',
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()['owner_id'] == str(owner_id)


def test_internal_auth_rejects_insufficient_scope(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'viewer:read-token:read',
    )
    get_settings.cache_clear()

    response = client.post(
        '/accounts',
        headers={'Authorization': 'Bearer read-token'},
        params={
            'account_type': AccountType.USER,
            'owner_id': uuid4(),
            'name': 'Test Account',
        },
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == {
        'detail': {
            'code': 'insufficient_internal_scope',
            'message': 'Internal service token does not allow this operation',
        }
    }


def test_internal_auth_keeps_metrics_public(client, monkeypatch):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'pls:test-token:read,write',
    )
    get_settings.cache_clear()

    response = client.get('/metrics')

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.parametrize('path', ['/docs', '/openapi.json', '/redoc'])
def test_internal_auth_keeps_api_documentation_public(
    client, monkeypatch, path
):
    monkeypatch.setenv(
        'LEDGER_INTERNAL_API_TOKENS',
        'pls:test-token:read,write',
    )
    get_settings.cache_clear()

    response = client.get(path)

    assert response.status_code == status.HTTP_200_OK


def test_openapi_documents_internal_service_token(client):
    schema = client.get('/openapi.json').json()

    assert schema['components']['securitySchemes']['InternalServiceToken'] == {
        'type': 'apiKey',
        'in': 'header',
        'name': 'X-Internal-Service-Token',
        'description': 'Token used for service-to-service Ledger API calls.',
    }
    assert schema['paths']['/accounts']['post']['security'] == [
        {'InternalServiceToken': []}
    ]
    assert 'security' not in schema['paths']['/']['get']


def test_create_user_account(client):
    owner_id = uuid4()
    response = client.post(
        '/accounts',
        params={
            'account_type': AccountType.USER,
            'owner_id': owner_id,
            'name': 'Test Account',
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    account_id = response.json().get('account_id')
    assert response.json() == {
        'account_id': str(account_id),
        'account_type': 'user',
        'owner_id': str(owner_id),
        'name': 'Test Account',
    }


def test_get_account_balance_returns_balance_fields(client, user_account):

    response = client.get(f'/accounts/{user_account.id}/balance')

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        'balance': user_account.balance,
        'reserved_balance': user_account.reserved_balance,
        'available_balance': max(
            user_account.balance - user_account.reserved_balance, 0
        ),
    }


def test_get_account_statement_returns_empty_entries_list(client):
    account_id = uuid4()

    response = client.get(f'/accounts/{account_id}/statement')

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {'entries': []}


@pytest.mark.asyncio
async def test_get_account_balance_function_success(session, user_account):

    result = await get_account_balance(user_account.id, session)

    assert result.model_dump() == {
        'balance': user_account.balance,
        'reserved_balance': user_account.reserved_balance,
        'available_balance': max(
            user_account.balance - user_account.reserved_balance, 0
        ),
    }


@pytest.mark.asyncio
async def test_get_account_balance_function_not_found(session):
    account_id = uuid4()
    with pytest.raises(HTTPException) as exc:
        await get_account_balance(account_id, session)

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
    assert exc.value.detail == 'Account not found'


@pytest.mark.asyncio
async def test_create_user_account_success(session):
    owner_id = uuid4()
    result = await create_account(
        session,
        account_type=AccountType.USER,
        owner_id=owner_id,
        name='Test Account',
    )

    assert result.model_dump() == {
        'account_id': result.account_id,
        'account_type': 'user',
        'owner_id': owner_id,
        'name': 'Test Account',
    }


@pytest.mark.asyncio
async def test_create_user_account_function_bad_request(session):
    with pytest.raises(HTTPException) as exc:
        await create_account(
            session,
            account_type=AccountType.USER,
            owner_id=None,
            name='Test Account',
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        exc.value.detail
        == 'owner_id is required when account_type is provided'
    )


@pytest.mark.asyncio
async def test_get_account_statement_success_empty(session, user_account):

    result = await get_account_statement(user_account.id, session)

    assert result.model_dump() == {
        'entries': [],
    }


@pytest.mark.asyncio
async def test_account_statement_serializes_persisted_entries(
    session,
    user_account,
    transaction,
):
    session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=user_account.id,
            entry_type='credit',
            amount=1000,
            description='Simulated payment',
        )
    )
    await session.commit()
    statement = await get_account_statement(user_account.id, session)
    assert statement.model_dump(mode='json')['entries'] == [
        {
            'account_id': str(user_account.id),
            'entry_type': 'credit',
            'amount': 1000,
            'description': 'Simulated payment',
            'reference_type': '',
            'reference_id': None,
        }
    ]
