import hashlib
import secrets
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.auth import (
    SESSION_USER_KEY,
    current_user_sub,
    internal_roles_for_sub,
    is_valid_user_info,
)
from hub.ledger_client import LedgerClient
from hub.models.database import get_session
from hub.models.tables import ApplicationAudit, ConnectedApplication


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{1,79}$')
    destination_account_id: UUID
    website_url: HttpUrl


def issue_key(application: ConnectedApplication) -> str:
    key = f'lep_{application.id.hex}.{secrets.token_urlsafe(32)}'
    application.key_hash = hashlib.sha256(key.encode()).hexdigest()
    application.key_prefix = key[:16]
    return key


async def require_admin(session: AsyncSession) -> str:
    actor = current_user_sub()
    if not actor or 'admin' not in await internal_roles_for_sub(
        actor, session
    ):
        raise HTTPException(403, 'Administrator access required')
    return actor


async def create_application(
    payload: ApplicationCreate,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
) -> tuple[ConnectedApplication, str]:
    actor = await require_admin(session)
    if await session.scalar(
        select(ConnectedApplication).where(
            ConnectedApplication.slug == payload.slug
        )
    ):
        raise HTTPException(409, 'Application identifier already exists')
    await (ledger or LedgerClient()).get_balance(
        payload.destination_account_id
    )
    application = ConnectedApplication(
        name=payload.name,
        slug=payload.slug,
        destination_account_id=payload.destination_account_id,
        website_url=str(payload.website_url),
        key_hash='',
        key_prefix='',
        created_by=actor,
    )
    key = issue_key(application)
    session.add(application)
    await session.flush()
    session.add(
        ApplicationAudit(
            application_id=application.id,
            actor_sub=actor,
            action='created',
        )
    )
    await session.commit()
    return application, key


async def change_application(
    application_id: UUID,
    action: str,
    session: AsyncSession,
) -> str | None:
    actor = await require_admin(session)
    application = await session.get(ConnectedApplication, application_id)
    if application is None:
        raise HTTPException(404, 'Application not found')
    key = None
    if action == 'rotate_key':
        key = issue_key(application)
    elif action in {'enabled', 'disabled'}:
        application.is_active = action == 'enabled'
    else:
        raise ValueError('Unknown application action')
    session.add(
        ApplicationAudit(
            application_id=application.id,
            actor_sub=actor,
            action=action,
        )
    )
    await session.commit()
    return key


async def require_application(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectedApplication:
    authorization = request.headers.get('Authorization', '')
    scheme, _, key = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not key.startswith('lep_'):
        raise HTTPException(401, 'Application API key required')
    try:
        application_id = UUID(hex=key.removeprefix('lep_').split('.')[0])
    except ValueError as exc:
        raise HTTPException(401, 'Invalid application API key') from exc
    application = await session.get(ConnectedApplication, application_id)
    digest = hashlib.sha256(key.encode()).hexdigest()
    if not application or not secrets.compare_digest(
        application.key_hash, digest
    ):
        raise HTTPException(401, 'Invalid application API key')
    if not application.is_active:
        raise HTTPException(403, 'Application is disabled')
    return application


def validate_checkout_application(payload, application: ConnectedApplication):
    if (
        payload.game_id != application.slug
        or payload.destination_account_id != application.destination_account_id
    ):
        raise HTTPException(
            403, 'Checkout must use the registered application and destination'
        )
    origin = urlsplit(application.website_url)
    for url in (payload.return_url, payload.cancel_url):
        parsed = urlsplit(str(url))
        if (
            (parsed.scheme, parsed.hostname, parsed.port)
            != (origin.scheme, origin.hostname, origin.port)
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(
                422,
                'Return and cancel URLs must use the registered origin',
            )


def require_checkout_user(request: Request) -> str:
    user = request.session.get(SESSION_USER_KEY)
    if not isinstance(user, dict) or not is_valid_user_info(user):
        raise HTTPException(401, 'Login is required to confirm payment')
    if request.headers.get('origin', '').rstrip('/') != str(
        request.base_url
    ).rstrip('/'):
        raise HTTPException(
            403, 'Confirm payment from the Leprechaun checkout'
        )
    return str(user['sub'])
