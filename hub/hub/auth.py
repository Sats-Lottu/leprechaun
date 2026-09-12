import logging
import time
from collections.abc import MutableMapping
from typing import Any
from uuid import UUID

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Request
from nicegui import app
from sqlalchemy import select
from starlette.responses import RedirectResponse

from hub.models.database import session_scope
from hub.models.enums import AdminRoleStatus
from hub.models.tables import AdminRoleAssignment
from hub.settings import Settings, get_settings

SESSION_USER_KEY = 'user_info'
SESSION_ROLES_KEY = 'roles'
ADMIN_ROLE = 'admin'
log = logging.getLogger(__name__)


def create_oauth(settings: Settings | None = None) -> OAuth:
    settings = settings or get_settings()
    token_auth_method = (
        'client_secret_basic' if settings.OIDC_CLIENT_SECRET else 'none'
    )
    oauth = OAuth()
    oauth.register(
        name='oidc',
        server_metadata_url=settings.OIDC_SERVER_METADATA_URL,
        client_id=settings.OIDC_CLIENT_ID,
        client_secret=settings.OIDC_CLIENT_SECRET,
        client_kwargs={
            'scope': settings.OIDC_SCOPE,
            'token_endpoint_auth_method': token_auth_method,
            'code_challenge_method': 'S256',
        },
    )
    return oauth


oauth = create_oauth()


def current_user() -> dict[str, Any] | None:
    user_info = _auth_storage().get(SESSION_USER_KEY)
    if not isinstance(user_info, dict):
        return None

    settings = get_settings()
    if not is_valid_user_info(user_info, settings=settings):
        clear_user()
        return None

    return user_info


def current_user_sub() -> str | None:
    user = current_user()
    if user is None:
        return None
    sub = user.get('sub')
    return str(sub) if sub else None


def current_roles() -> set[str]:
    if current_user_sub() is None:
        return set()

    return stored_roles()


def has_role(role: str) -> bool:
    normalized_role = normalize_role(role)
    return bool(normalized_role) and normalized_role in current_roles()


def is_admin() -> bool:
    return has_role(ADMIN_ROLE)


def store_user(
    user_info: dict[str, Any], request: Request | None = None
) -> bool:
    if not is_valid_user_info(user_info):
        return False
    storage = _auth_storage(request)
    storage[SESSION_USER_KEY] = user_info
    storage[SESSION_ROLES_KEY] = []
    return True


async def store_user_with_internal_roles(
    user_info: dict[str, Any], request: Request | None = None
) -> bool:
    if not store_user(user_info, request=request):
        return False

    async with session_scope() as session:
        roles = await internal_roles_for_sub(str(user_info['sub']), session)
    _auth_storage(request)[SESSION_ROLES_KEY] = sorted(roles)
    return True


def clear_user(request: Request | None = None) -> None:
    storage = _auth_storage(request)
    storage.pop(SESSION_USER_KEY, None)
    storage.pop(SESSION_ROLES_KEY, None)


async def login(request: Request) -> RedirectResponse:
    request.session.pop('checkout_after_login', None)
    checkout_id = request.query_params.get('checkout_session')
    if checkout_id:
        try:
            request.session['checkout_after_login'] = str(UUID(checkout_id))
        except ValueError:
            pass
    settings = get_settings()
    client = oauth.create_client('oidc')
    callback_url = request.url_for('oidc_callback')
    if settings.OIDC_REDIRECT_PATH:
        callback_url = str(request.base_url).rstrip('/')
        callback_url = f'{callback_url}{settings.OIDC_REDIRECT_PATH}'

    return await client.authorize_redirect(request, callback_url)


async def callback(request: Request) -> RedirectResponse:
    checkout_id = request.session.pop('checkout_after_login', None)
    try:
        client = oauth.create_client('oidc')
        token = await client.authorize_access_token(request)
        user_info = dict(token.get('userinfo') or {})
        stored = await store_user_with_internal_roles(
            user_info, request=request
        )
        if stored and checkout_id:
            return RedirectResponse(
                f'/user/checkout?session_id={UUID(checkout_id)}'
            )
    except (OAuthError, Exception):
        log.exception('could not authorize access token')

    return RedirectResponse('/')


def logout(request: Request) -> RedirectResponse:
    clear_user(request)
    return RedirectResponse('/')


def is_valid_user_info(
    user_info: dict[str, Any],
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> bool:
    settings = settings or get_settings()
    now = now if now is not None else int(time.time())

    if not user_info.get('sub'):
        return False

    exp = int(user_info.get('exp', 0))
    if exp <= now:
        return False

    if not _audience_matches(user_info.get('aud'), settings.OIDC_CLIENT_ID):
        return False

    issuer = str(user_info.get('iss', ''))
    return issuer == settings.OIDC_ISSUER


async def internal_roles_for_sub(user_sub: str, session) -> set[str]:
    result = await session.scalars(
        select(AdminRoleAssignment.role).where(
            AdminRoleAssignment.user_sub == user_sub,
            AdminRoleAssignment.status == AdminRoleStatus.ACTIVE,
        )
    )
    return {normalize_role(role) for role in result if normalize_role(role)}


def stored_roles() -> set[str]:
    roles = _auth_storage().get(SESSION_ROLES_KEY, [])
    if not isinstance(roles, list | tuple | set):
        return set()
    return {
        normalized_role
        for role in roles
        if (normalized_role := normalize_role(str(role)))
    }


def normalize_role(role: str) -> str:
    return role.strip().lower()


def _audience_matches(audience: object, client_id: str) -> bool:
    if not client_id:
        return False
    if isinstance(audience, str):
        return audience == client_id
    if isinstance(audience, list):
        return client_id in {str(item) for item in audience}
    return False


def _auth_storage(
    request: Request | None = None,
) -> MutableMapping[str, Any]:
    if request is not None:
        return request.session
    try:
        return app.storage.browser
    except (AttributeError, RuntimeError):
        return app.storage.user
