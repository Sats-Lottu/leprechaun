"""Development only: provision SatOIDC demo identities and the Hub client."""

import asyncio
import os
import subprocess
from uuid import UUID

from satoidc.auth.security import hash_password
from satoidc.models import OAuth2Client, User
from satoidc.models.database import engine
from satoidc.services.oauth_clients import build_client_metadata
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def seed() -> None:
    async with AsyncSession(engine) as session, session.begin():
        player_id = UUID('00000000-0000-0000-0000-000000000300')
        if await session.get(User, player_id) is None:
            player = User(
                lnurl_pubkey=None,
                email='player@example.com',
                login='player',
                password_hash=hash_password('Local-player-123!'),
                nickname='Local Player',
                email_verified=True,
            )
            player.id = player_id
            session.add(player)
            await session.flush()
        client = await session.scalar(
            select(OAuth2Client).where(
                OAuth2Client.client_id == 'leprechaun-hub'
            )
        )
        if client is None:
            client = OAuth2Client(
                user_id=player_id,
                client_id='leprechaun-hub',
                client_secret='',
            )
            client.set_client_metadata(
                build_client_metadata(
                    client_name='Leprechaun Hub (local)',
                    client_uri='http://localhost:8000',
                    scope='openid profile',
                    redirect_uri='http://localhost:8000/auth/callback',
                    grant_type='authorization_code',
                    response_type='code',
                    token_endpoint_auth_method='none',
                )
            )
            session.add(client)
    await engine.dispose()


def main() -> None:
    if os.environ.get('APP_ENV') != 'development':
        raise SystemExit('The local bootstrap requires APP_ENV=development.')
    subprocess.run(['poetry', 'run', 'alembic', 'upgrade', 'head'], check=True)
    subprocess.run(
        [
            'poetry',
            'run',
            'python',
            '-m',
            'setup_wizard.bootstrap',
            '--database-state',
        ],
        check=True,
    )
    asyncio.run(seed())
    os.execvp(
        'poetry',
        [
            'poetry',
            'run',
            'fastapi',
            'run',
            '--host',
            '0.0.0.0',
            '--port',
            '8000',
            'satoidc/main.py',
        ],
    )


if __name__ == '__main__':
    main()
