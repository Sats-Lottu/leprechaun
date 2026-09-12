from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from authlib.integrations.starlette_client import OAuthError
from starlette.responses import RedirectResponse

from hub import auth
from hub.auth import is_valid_user_info
from hub.models.tables import AdminRoleAssignment
from hub.settings import Settings

REDIRECT_STATUS = 307


@pytest.mark.parametrize(
    'checkout_id',
    [
        '00000000-0000-0000-0000-000000000123',
        'https://attacker.example',
    ],
)
async def test_login_preserves_only_checkout_identifiers(
    monkeypatch, checkout_id
):
    client = SimpleNamespace(
        authorize_redirect=AsyncMock(return_value=RedirectResponse('/oidc')),
    )
    monkeypatch.setattr(
        auth, 'oauth', SimpleNamespace(create_client=lambda name: client)
    )
    request = SimpleNamespace(
        session={},
        query_params={'checkout_session': checkout_id},
        base_url='http://hub.local/',
        url_for=lambda name: 'http://hub.local/auth/callback',
    )
    await auth.login(request)
    if checkout_id.startswith('https:'):
        assert 'checkout_after_login' not in request.session
    else:
        assert request.session['checkout_after_login'] == checkout_id


async def test_callback_returns_to_checkout_after_login(monkeypatch):
    checkout_id = '00000000-0000-0000-0000-000000000123'
    client = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                'userinfo': {'sub': 'player'},
            }
        )
    )
    monkeypatch.setattr(
        auth, 'oauth', SimpleNamespace(create_client=lambda name: client)
    )
    monkeypatch.setattr(
        auth, 'store_user_with_internal_roles', AsyncMock(return_value=True)
    )
    request = SimpleNamespace(session={'checkout_after_login': checkout_id})
    response = await auth.callback(request)
    assert (
        response.headers['location']
        == f'/user/checkout?session_id={checkout_id}'
    )
    assert 'checkout_after_login' not in request.session


def test_valid_user_info_uses_sub_as_identity() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 200,
            'aud': ['hub'],
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_without_sub_is_invalid() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert not is_valid_user_info(
        {
            'exp': 200,
            'aud': ['hub'],
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_rejects_wrong_audience() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert not is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 200,
            'aud': ['other-client'],
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_accepts_string_audience() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 200,
            'aud': 'hub',
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_rejects_expired_token() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert not is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 99,
            'aud': 'hub',
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_rejects_wrong_issuer() -> None:
    settings = Settings(
        OIDC_CLIENT_ID='hub',
        OIDC_ISSUER='http://oidc.local',
    )

    assert not is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 200,
            'aud': 'hub',
            'iss': 'http://wrong.local',
        },
        settings=settings,
        now=100,
    )


def test_user_info_rejects_missing_client_id() -> None:
    settings = Settings(OIDC_CLIENT_ID='', OIDC_ISSUER='http://oidc.local')

    assert not is_valid_user_info(
        {
            'sub': 'user-123',
            'exp': 200,
            'aud': 'hub',
            'iss': 'http://oidc.local',
        },
        settings=settings,
        now=100,
    )


def test_current_user_removes_invalid_session(monkeypatch) -> None:
    user_storage = {
        auth.SESSION_USER_KEY: {
            'sub': 'user-123',
            'exp': 99,
            'aud': 'hub',
            'iss': 'http://oidc.local',
        }
    }
    monkeypatch.setattr(
        auth,
        'app',
        SimpleNamespace(storage=SimpleNamespace(user=user_storage)),
    )
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_ISSUER='http://oidc.local',
        ),
    )

    assert auth.current_user() is None
    assert auth.SESSION_USER_KEY not in user_storage


def test_current_user_sub_returns_sub(monkeypatch) -> None:
    monkeypatch.setattr(auth, 'current_user', lambda: {'sub': 'user-123'})

    assert auth.current_user_sub() == 'user-123'


def test_current_user_sub_returns_none_without_user(monkeypatch) -> None:
    monkeypatch.setattr(auth, 'current_user', lambda: None)

    assert auth.current_user_sub() is None


def test_current_roles_returns_internal_roles(monkeypatch) -> None:
    user_storage = {
        auth.SESSION_USER_KEY: {
            'sub': 'admin-123',
            'exp': 4_102_444_800,
            'aud': 'hub',
            'iss': 'http://oidc.local',
        },
        auth.SESSION_ROLES_KEY: ['Admin', 'support'],
    }
    monkeypatch.setattr(
        auth,
        'app',
        SimpleNamespace(storage=SimpleNamespace(user=user_storage)),
    )
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_ISSUER='http://oidc.local',
        ),
    )

    assert auth.current_roles() == {'admin', 'support'}
    assert auth.has_role('admin')
    assert auth.is_admin()


def test_current_roles_requires_authenticated_user(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        'app',
        SimpleNamespace(
            storage=SimpleNamespace(user={auth.SESSION_ROLES_KEY: 'admin'})
        ),
    )

    assert auth.current_roles() == set()
    assert not auth.has_role('admin')
    assert not auth.is_admin()


@pytest.mark.asyncio
async def test_internal_roles_are_loaded_from_database(session) -> None:
    session.add_all([
        AdminRoleAssignment(user_sub='admin-123', role='admin'),
        AdminRoleAssignment(user_sub='admin-123', role='support'),
        AdminRoleAssignment(
            user_sub='admin-123',
            role='viewer',
            status='revoked',
        ),
        AdminRoleAssignment(user_sub='user-123', role='viewer'),
    ])
    await session.commit()

    assert await auth.internal_roles_for_sub('admin-123', session) == {
        'admin',
        'support',
    }
    assert await auth.internal_roles_for_sub('user-123', session) == {'viewer'}
    assert await auth.internal_roles_for_sub('missing', session) == set()


def test_store_and_clear_user(monkeypatch) -> None:
    user_storage = {}
    monkeypatch.setattr(
        auth,
        'app',
        SimpleNamespace(storage=SimpleNamespace(user=user_storage)),
    )
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_ISSUER='http://oidc.local',
        ),
    )

    stored = auth.store_user({
        'sub': 'user-123',
        'exp': 4_102_444_800,
        'aud': 'hub',
        'iss': 'http://oidc.local',
    })

    assert stored
    assert user_storage[auth.SESSION_USER_KEY]['sub'] == 'user-123'
    assert user_storage[auth.SESSION_ROLES_KEY] == []

    auth.clear_user()

    assert auth.SESSION_USER_KEY not in user_storage
    assert auth.SESSION_ROLES_KEY not in user_storage


def test_clear_user_can_use_request_session() -> None:
    request = SimpleNamespace(
        session={
            auth.SESSION_USER_KEY: {'sub': 'user-123'},
            auth.SESSION_ROLES_KEY: ['admin'],
            'id': 'session-123',
        }
    )

    auth.clear_user(request)

    assert auth.SESSION_USER_KEY not in request.session
    assert auth.SESSION_ROLES_KEY not in request.session
    assert request.session['id'] == 'session-123'


def test_store_user_ignores_oidc_role_claims(monkeypatch) -> None:
    user_storage = {}
    monkeypatch.setattr(
        auth,
        'app',
        SimpleNamespace(storage=SimpleNamespace(user=user_storage)),
    )
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_ISSUER='http://oidc.local',
        ),
    )

    stored = auth.store_user({
        'sub': 'user-123',
        'exp': 4_102_444_800,
        'aud': 'hub',
        'iss': 'http://oidc.local',
        'roles': ['admin'],
    })

    assert stored
    assert user_storage[auth.SESSION_ROLES_KEY] == []


def test_create_oauth_uses_public_client_without_secret() -> None:
    oauth = auth.create_oauth(
        Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_CLIENT_SECRET=None,
        )
    )

    client = oauth._clients['oidc']  # noqa: SLF001

    assert client.client_kwargs['token_endpoint_auth_method'] == 'none'


def test_create_oauth_uses_confidential_client_with_secret() -> None:
    oauth = auth.create_oauth(
        Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_CLIENT_SECRET='secret',
        )
    )

    client = oauth._clients['oidc']  # noqa: SLF001

    assert (
        client.client_kwargs['token_endpoint_auth_method']
        == 'client_secret_basic'
    )


@pytest.mark.asyncio
async def test_login_redirects_to_configured_callback(monkeypatch) -> None:
    authorize_redirect = AsyncMock(return_value=RedirectResponse('/provider'))
    client = SimpleNamespace(authorize_redirect=authorize_redirect)
    monkeypatch.setattr(
        auth,
        'oauth',
        SimpleNamespace(create_client=lambda name: client),
    )
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: Settings(
            OIDC_CLIENT_ID='hub',
            OIDC_REDIRECT_PATH='/auth/callback',
        ),
    )
    request = SimpleNamespace(
        session={},
        query_params={},
        base_url='http://hub.local/',
        url_for=lambda name: f'http://hub.local/{name}',
    )

    response = await auth.login(request)

    assert response.status_code == REDIRECT_STATUS
    authorize_redirect.assert_awaited_once_with(
        request,
        'http://hub.local/auth/callback',
    )


@pytest.mark.asyncio
async def test_callback_stores_userinfo(monkeypatch) -> None:
    stored = {}

    async def store_user(data, request=None) -> None:
        stored.update(data)
        stored['request'] = request
        return True

    client = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={'userinfo': {'sub': 'user-123'}}
        )
    )
    monkeypatch.setattr(
        auth,
        'oauth',
        SimpleNamespace(create_client=lambda name: client),
    )
    monkeypatch.setattr(auth, 'store_user_with_internal_roles', store_user)

    request = SimpleNamespace(session={})
    response = await auth.callback(request)

    assert response.status_code == REDIRECT_STATUS
    assert stored['sub'] == 'user-123'
    assert stored['request'] is request


@pytest.mark.asyncio
async def test_callback_stores_empty_userinfo_when_token_has_no_userinfo(
    monkeypatch,
) -> None:
    stored = {}

    async def store_user(data, request=None) -> None:
        stored.update(data)
        stored['request'] = request
        return False

    client = SimpleNamespace(
        authorize_access_token=AsyncMock(return_value={'id_token': 'token'})
    )
    monkeypatch.setattr(
        auth,
        'oauth',
        SimpleNamespace(create_client=lambda name: client),
    )
    monkeypatch.setattr(auth, 'store_user_with_internal_roles', store_user)

    request = SimpleNamespace(session={})
    response = await auth.callback(request)

    assert response.status_code == REDIRECT_STATUS
    assert stored['request'] is request
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_callback_ignores_oauth_error(monkeypatch) -> None:
    client = SimpleNamespace(
        authorize_access_token=AsyncMock(
            side_effect=OAuthError(error='invalid_grant')
        )
    )
    monkeypatch.setattr(
        auth,
        'oauth',
        SimpleNamespace(create_client=lambda name: client),
    )

    response = await auth.callback(SimpleNamespace(session={}))

    assert response.status_code == REDIRECT_STATUS


def test_logout_clears_request_session() -> None:
    request = SimpleNamespace(
        session={
            auth.SESSION_USER_KEY: {'sub': 'user-123'},
            auth.SESSION_ROLES_KEY: ['admin'],
            'id': 'session-123',
        }
    )

    response = auth.logout(request)

    assert response.status_code == REDIRECT_STATUS
    assert auth.SESSION_USER_KEY not in request.session
    assert auth.SESSION_ROLES_KEY not in request.session
    assert request.session['id'] == 'session-123'
